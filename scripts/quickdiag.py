import geopandas as gpd
from shapely.geometry import box

# Load basin
basin = gpd.read_file("data/basins/cagayan.geojson")
print("Basin CRS:", basin.crs)
print("Basin total bounds:", basin.total_bounds)
print("Basin is valid:", basin.geometry.is_valid.all())

# Load PhilSA shapefile
philsa = gpd.read_file("data/philsa_shapefiles/cagayan_2024-10-27.shp")
print("PhilSA CRS:", philsa.crs)
print("PhilSA total bounds:", philsa.total_bounds)

# Reproject both to UTM zone 51N (EPSG:32651) – same as pipeline's working CRS
basin_utm = basin.to_crs("EPSG:32651")
# Get the actual polygon geometry (first feature)
basin_geom = basin_utm.geometry.iloc[0]

# PhilSA bounding box as a polygon, reprojected
philsa_box = box(*philsa.total_bounds)
philsa_box_utm = gpd.GeoSeries([philsa_box], crs=philsa.crs).to_crs("EPSG:32651").iloc[0]

# Intersection
intersection = basin_geom.intersection(philsa_box_utm)
print("Intersection area (m²):", intersection.area if not intersection.is_empty else 0)
print("Is intersection empty?", intersection.is_empty)

# Also compute the basin area and PhilSA bounding box area for reference
print("Basin area (m²):", basin_geom.area)
print("PhilSA bbox area (m²):", philsa_box_utm.area)