import ee
import geopandas as gpd
import geemap
from shapely.geometry import box

ee.Initialize(project="flood-thesis-507015")

# 1. Load the bounding‑box basin
gdf = gpd.read_file("data/basins/cagayan_bbox.geojson")
print("Basin CRS:", gdf.crs)
print("Basin geometry:", gdf.geometry.iloc[0])

# Convert to EE
fc = geemap.geopandas_to_ee(gdf)
ee_geom = fc.geometry()
print("EE geometry type:", ee_geom.getInfo()['type'])
print("EE geometry area (m²):", ee_geom.area().getInfo())

# 2. Load PhilSA shapefile (just its bounding box)
philsa = gpd.read_file("data/philsa_shapefiles/cagayan_2024-10-27.shp")
philsa_bbox = box(*philsa.total_bounds)
philsa_gdf = gpd.GeoDataFrame({'geometry': [philsa_bbox]}, crs=philsa.crs)
philsa_fc = geemap.geopandas_to_ee(philsa_gdf)
philsa_geom = philsa_fc.geometry()

# 3. Intersection (this is what the pipeline uses as work_region)
intersection = ee_geom.intersection(philsa_geom)
print("Intersection area (m²):", intersection.area().getInfo())

# 4. Test patch grid generation on this intersection
from patching import generate_patch_grid   # if available, else just manual
# Or we can just try to generate a simple grid using ee.List
# But let's first see if intersection is non‑empty.