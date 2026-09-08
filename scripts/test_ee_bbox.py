import ee
import geopandas as gpd
import geemap

ee.Initialize(project="flood-thesis-507015")

# Load the bbox
gdf = gpd.read_file("data/basins/cagayan_bbox.geojson")
# Convert to EE
ee_geom = geemap.geopandas_to_ee(gdf).geometry()
print("EE geometry type:", ee_geom.getInfo()['type'])
print("EE geometry area (m²):", ee_geom.area().getInfo())

# Also test the working region (intersection with PhilSA mask bbox)
philsa_bbox = ee.Geometry.Rectangle([121.461, 16.957, 122.050, 18.018])
intersection = ee_geom.intersection(philsa_bbox)
print("Intersection area (m²):", intersection.area().getInfo())