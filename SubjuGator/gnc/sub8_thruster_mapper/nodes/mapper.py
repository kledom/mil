#!/usr/bin/env python
import rospy
from scipy.optimize import minimize
import numpy as np
from mil_ros_tools import wait_for_param, thread_lock, rosmsg_to_numpy, msg_helpers
import threading
from sub8_msgs.msg import Thrust, ThrusterCmd
from sub8_msgs.srv import (UpdateThrusterLayout, UpdateThrusterLayoutResponse,
                           BMatrix, BMatrixResponse)
from geometry_msgs.msg import WrenchStamped
lock = threading.Lock()


class ThrusterMapper(object):
    _min_command_time = rospy.Duration(0.05)
    min_commandable_thrust = 1e-2  # Newtons

    def __init__(self):
        '''The layout should be a dictionary of the form used in thruster_mapper.launch
        an excerpt is quoted here for posterity

        thrusters:
          FLH: {
            motor_id:  0,
            position:  [0.2678, 0.2795, 0.0],
            direction: [-0.866, 0.5, 0.0],
            bounds:    [-90.0, 90.0]
          }
          FLV: {
            ...
          }
          ...
        '''
        self.num_thrusters = 0
        rospy.init_node('thruster_mapper')
        self.last_command_time = rospy.Time.now()
        self.thruster_layout = wait_for_param('thruster_layout')
        self.thruster_name_map = []
        self.dropped_thrusters = []
        self.B = self.generate_B(self.thruster_layout)
        self.Binv = np.linalg.pinv(self.B)
        self.min_thrusts, self.max_thrusts = self.get_ranges()
        self.default_min_thrusts, self.default_max_thrusts = np.copy(self.min_thrusts), np.copy(self.max_thrusts)
        self.update_layout_server = rospy.Service('update_thruster_layout', UpdateThrusterLayout,
                                                  self.update_layout)

        # Expose B matrix through a srv
        self.b_matrix_server = rospy.Service('b_matrix', BMatrix, self.get_b_matrix)

        self.wrench_sub = rospy.Subscriber('wrench', WrenchStamped, self.request_wrench_cb, queue_size=1)
        self.actual_wrench_pub = rospy.Publisher('wrench_actual', WrenchStamped, queue_size=1)
        self.wrench_error_pub = rospy.Publisher('wrench_error', WrenchStamped, queue_size=1)
        self.thruster_pub = rospy.Publisher('thrusters/thrust', Thrust, queue_size=1)

    @thread_lock(lock)
    def update_layout(self, srv):
        '''Update the physical thruster layout.
        This should only be done in a thruster-out event
        '''
        rospy.logwarn("Layout in update...")
        self.dropped_thrusters = srv.dropped_thrusters
        rospy.logwarn("Missing thrusters: {}".format(self.dropped_thrusters))

        # Reset min and max thrusts, this will be overwritten by any dropped thrusters
        self.min_thrusts = np.copy(self.default_min_thrusts)
        self.max_thrusts = np.copy(self.default_max_thrusts)

        for thruster_name in self.dropped_thrusters:
            thruster_index = self.thruster_name_map.index(thruster_name)
            self.min_thrusts[thruster_index] = -self.min_commandable_thrust * 0.5
            self.max_thrusts[thruster_index] = self.min_commandable_thrust * 0.5

        rospy.logwarn("Layout updated")
        return UpdateThrusterLayoutResponse()

    def get_ranges(self):
        '''Get upper and lower thrust limits for each thruster
        '''
        minima = np.array([self.thruster_layout['thrusters'][x]['thrust_bounds'][0]
                           for x in self.thruster_name_map])
        maxima = np.array([self.thruster_layout['thrusters'][x]['thrust_bounds'][1]
                           for x in self.thruster_name_map])
        return minima, maxima

    def get_thruster_wrench(self, position, direction):
        '''Compute a single column of B, or the wrench created by a particular thruster'''
        assert np.isclose(1.0, np.linalg.norm(direction), atol=1e-3), "Direction must be a unit vector"
        forces = direction
        torques = np.cross(position, forces)
        wrench_column = np.hstack([forces, torques])
        return np.transpose(wrench_column)

    def get_b_matrix(self, srv):
        ''' Return a copy of the B matrix flattened into a 1-D row-major order list '''
        return BMatrixResponse(self.B.flatten())

    def generate_B(self, layout):
        '''Construct the control-input matrix
        Each column represents the wrench generated by a single thruster

        The single letter "B" is conventionally used to refer to a matrix which converts
         a vector of control inputs to a wrench

        Meaning where u = [thrust_1, ... thrust_n],
         B * u = [Fx, Fy, Fz, Tx, Ty, Tz]
        '''
        # Maintain an ordered list, tracking which column corresponds to which thruster
        self.thruster_name_map = []
        self.thruster_bounds = []
        B = []
        self.num_thrusters = 0
        for thruster_name, thruster_info in layout['thrusters'].items():
            # Assemble the B matrix by columns
            self.thruster_name_map.append(thruster_name)
            wrench_column = self.get_thruster_wrench(
                thruster_info['position'],
                thruster_info['direction']
            )
            self.num_thrusters += 1
            B.append(wrench_column)
        return np.transpose(np.array(B))

    def map(self, wrench):
        '''TODO:
            - Account for variable thrusters
        '''
        thrust_cost = np.diag([1E-4] * self.num_thrusters)

        def objective(u):
            '''Tikhonov-regularized least-squares cost function
               https://en.wikipedia.org/wiki/Tikhonov_regularization
            Minimize
                norm((B * u) - wrench) + (u.T * R * u)
            Subject to
                min_u < u < max_u
            Where
                R defines the cost of firing the thrusters
                u is a vector where u[n] is the thrust output by thruster_n

            We make R as small as possible to avoid leaving the solution space of
            B*u = wrench.
            '''
            error_cost = np.linalg.norm(self.B.dot(u) - wrench) ** 2
            effort_cost = np.transpose(u).dot(thrust_cost).dot(u)
            return error_cost + effort_cost

        def obj_jacobian(u):
            '''Compute the jacobian of the objective function

            [1] Scalar-By-Matrix derivative identities [Online]
                Available: https://en.wikipedia.org/wiki/Matrix_calculus#Scalar-by-vector_identities
            '''
            error_jacobian = 2 * self.B.T.dot(self.B.dot(u) - wrench)
            effort_jacobian = np.transpose(u).dot(2 * thrust_cost)
            return error_jacobian + effort_jacobian

        # Initialize minimization at the analytical solution of the unconstrained problem
        minimization = minimize(
            method='slsqp',
            fun=objective,
            jac=obj_jacobian,
            x0=np.clip(self.Binv.dot(wrench), self.min_thrusts, self.max_thrusts),
            bounds=zip(self.min_thrusts, self.max_thrusts),
            tol=1E-6
        )
        return minimization.x, minimization.success

    @thread_lock(lock)
    def request_wrench_cb(self, msg):
        '''Callback for requesting a wrench'''
        time_now = rospy.Time.now()
        if (time_now - self.last_command_time) < self._min_command_time:
            return
        else:
            self.last_command_time = rospy.Time.now()

        force = rosmsg_to_numpy(msg.wrench.force)
        torque = rosmsg_to_numpy(msg.wrench.torque)
        wrench = np.hstack([force, torque])

        success = False
        while not success:
            u, success = self.map(wrench)
            if not success:
                # If we fail to compute, shrink the wrench
                wrench = wrench * 0.75
                continue

            thrust_cmds = []
            # Assemble the list of thrust commands to send
            for name, thrust in zip(self.thruster_name_map, u):
                # > Can speed this up by avoiding appends
                if name in self.dropped_thrusters:
                    thrust = 0  # Sending a command packet is an opportunity to detect thruster recovery

                # Simulate thruster deadband
                if np.fabs(thrust) < self.min_commandable_thrust:
                    thrust = 0

                thrust_cmds.append(ThrusterCmd(name=name, thrust=thrust))

        actual_wrench = self.B.dot(u)
        self.actual_wrench_pub.publish(
            msg_helpers.make_wrench_stamped(actual_wrench[:3], actual_wrench[3:], frame='/base_link')
        )
        mapper_wrench_error = wrench - actual_wrench
        self.wrench_error_pub.publish(
            msg_helpers.make_wrench_stamped(mapper_wrench_error[:3], mapper_wrench_error[3:], frame='/base_link')
        )
        self.thruster_pub.publish(thrust_cmds)


if __name__ == '__main__':
    mapper = ThrusterMapper()
    rospy.spin()