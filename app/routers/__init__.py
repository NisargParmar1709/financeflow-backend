"""
app/routers/__init__.py

NOTE: Do NOT re-export routers here- main.py imports each router module
ddorectly (e.g `from app.routers import auth`). Putting re-exports here 
causes a circular import because this __init__.py is part of the same
package being initialised.
"""