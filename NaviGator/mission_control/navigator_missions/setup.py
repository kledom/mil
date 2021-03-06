# ! DO NOT MANUALLY INVOKE THIS setup.py, USE CATKIN INSTEAD

from distutils.core import setup
from catkin_pkg.python_setup import generate_distutils_setup

# fetch values from package.xml
setup_args = generate_distutils_setup(
    packages=['navigator_missions', 'nav_missions_test', 'nav_missions_lib', 'vrx_missions']
)

setup(**setup_args)
