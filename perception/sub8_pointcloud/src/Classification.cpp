// TODO: Segmentation, Classification, Bounds, Ogrid filtering

#include "Classification.hpp"
Classification::Classification(ros::NodeHandle *nh)
{
  nh_ = nh;

  // TODO: Algorithms to filter pointcloud and possibly some classification scheme
}

pcl::PointCloud<pcl::PointXYZI>::Ptr Classification::filtered(pcl::PointCloud<pcl::PointXYZI>::ConstPtr pointCloud)
{
  pcl::PointCloud<pcl::PointXYZI>::Ptr cloud_filtered(new pcl::PointCloud<pcl::PointXYZI>);
  if (pointCloud->points.size() < 1)
    return cloud_filtered;
  pcl::StatisticalOutlierRemoval<pcl::PointXYZI> sor;
  sor.setInputCloud(pointCloud);
  sor.setMeanK(75);
  sor.setStddevMulThresh(.75);
  sor.filter(*cloud_filtered);
  return cloud_filtered;
}

// Get first incidient point in a ray. If no such point exist, return the starting point of the ray
cv::Point2d Classification::get_first_hit(cv::Mat &mat_ogrid, cv::Point2d start, float theta, int max_dis = 100)
{
  cv::Rect rect(cv::Point(0, 0), mat_ogrid.size());
  cv::Point2d vec_d_theta(cos(theta), sin(theta));
  for (int i = 0; i < max_dis; ++i)
  {
    cv::Point2d p_on_ray = vec_d_theta * i + start;
    if (!rect.contains(p_on_ray))
      return start;
    if (mat_ogrid.at<uchar>(p_on_ray.y, p_on_ray.x) == (uchar)WAYPOINT_ERROR_TYPE::OCCUPIED)
    {
      return p_on_ray;
    }
  }

  return start;
}

void Classification::fake_ogrid(cv::Mat &mat_ogrid, float resolution, const tf::StampedTransform &transform)
{
  // Sub's position relative to the ogrid
  cv::Point where_sub = cv::Point2d(transform.getOrigin().x() / resolution + mat_ogrid.cols / 2,
                                    transform.getOrigin().y() / resolution + mat_ogrid.rows / 2);
  cv::rectangle(mat_ogrid, cv::Point(-10, -10) + where_sub, cv::Point(15, 10) + where_sub,
                cv::Scalar((uchar)WAYPOINT_ERROR_TYPE::OCCUPIED), 2);
  cv::rectangle(mat_ogrid, cv::Point(-5, -5) + where_sub, cv::Point(12, 5) + where_sub, cv::Scalar(0), -1);
  cv::rectangle(mat_ogrid, cv::Point(5, -2) + where_sub, cv::Point(7, 0) + where_sub,
                cv::Scalar((uchar)WAYPOINT_ERROR_TYPE::OCCUPIED), -1);
}

/*
  Get first incident points in rays that are generated by changing angles and forming a circle.
  Then draw a filled polygon using those incident points
*/
void Classification::zonify(cv::Mat &mat_ogrid, float resolution, const tf::StampedTransform &transform)
{
  // Sub's position relative to the ogrid
  cv::Point2d where_sub = cv::Point2d(transform.getOrigin().x() / resolution + mat_ogrid.cols / 2,
                                      transform.getOrigin().y() / resolution + mat_ogrid.rows / 2);

  // List of intersections
  std::vector<cv::Point> intersections;
  intersections.push_back(where_sub);

  // Find first hits in an expanding circle
  for (float d_theta = 0.f; d_theta <= 2 * CV_PI; d_theta += 0.005)
  {
    cv::Point2d p_on_ray = get_first_hit(mat_ogrid, where_sub, d_theta, mat_ogrid.cols);
    intersections.push_back(cv::Point(p_on_ray.x, p_on_ray.y));
  }

  const cv::Point *pts = (const cv::Point *)cv::Mat(intersections).data;
  int npts = cv::Mat(intersections).rows;
  cv::fillPoly(mat_ogrid, &pts, &npts, 1, cv::Scalar((uchar)WAYPOINT_ERROR_TYPE::UNOCCUPIED));
}
