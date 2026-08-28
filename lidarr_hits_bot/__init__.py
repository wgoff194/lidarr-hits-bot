"""
Lidarr Hits Bot Package
"""

# Import main modules to make them available
from . import config
from . import database
from . import checker
from . import helpers
from . import commands
from . import views

__version__ = "1.0.0"
__all__ = [
    "config",
    "database", 
    "checker",
    "helpers",
    "commands",
    "views",
]