# app.py
import streamlit as st
import geopandas as gpd
import zipfile
import tempfile
import shutil
import os
import io
import uuid
import time
import glob
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, LineString, MultiLineString
from shapely.ops import unary_union, polygonize, linemerge
import folium
from streamlit_folium import st_folium

# ---------- Helpers ----------
def is_kmz(name: str):
    return name.lower().endswith(".kmz")

def is_kml(name: str):
    return name.lower().endswith(".kml")

def extract_kmz(kmz_path: str, dest_dir: str):
    with zipfile.ZipFile(kmz_path, "r") as z:
        z.extractall(dest_dir)
    for root, _, files in os.walk(dest_dir):
        for f in files:
            if f.lower().endswith(".kml"):
                return os.path.join(root, f)
    return None

def read_kml(kml_path: str):
    # try normal read, else explicit KML driver
    try:
        gdf = gpd.read_file(kml_path)
        if gdf is None or len(gdf) == 0:
            gdf = gpd.read_file(kml_path, driver="KML")
        return gdf
    except Exception:
        return gpd.read_file(kml_path, driver="KML")

def extract_polygons_from_geom(geom):
    out = []
    if geom is None:
        return out
    t = geom.geom_type
    if t == "Polygon":
        out.append(geom)
    elif t == "MultiPolygon":
        out.append(geom)
    elif t == "GeometryCollection":
        for part in geom.geoms:
            if part.geom_type in ("Polygon", "MultiPolygon"):
                out.append(part)
    return out

def filter_polygons_gdf(gdf: gpd.GeoDataFrame):
    rows = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        parts = extract_polygons_from_geom(geom)
        if not parts:
            continue
        for p in parts:
            new = row.copy()
            new.geometry = p
            rows.append(new)
    if not rows:
        return gpd.GeoDataFrame(columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    gdf_poly = gpd.GeoDataFrame(rows, columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    return gdf_poly.reset_index(drop=True)

def lines_to_polygons(gdf):
    """
    Try to create polygons from LineString / MultiLineString geometries.
    Approach:
      - collect all linear geometries
      - merge/union them into a network
      - run shapely.ops.polygonize on the network to get polygons
    Returns a GeoDataFrame with geometry column only (no attributes).
    """
    lines = []
    for geom in gdf.geometry:
        if geom is None:
            continue
        if geom.geom_type in ("LineString", "MultiLineString"):
            lines.append(geom)
        # Sometimes polygons are represented as closed LineString — include them too
        if geom.geom_type == "Polygon":
            # skip polygons (we handle polygons elsewhere)
            pass
        # GeometryCollection may contain lines
        if geom.geom_type == "GeometryCollection":
            for part in geom.geoms:
                if part.geom_type in ("LineString", "MultiLineString"):
                    lines.append(part)
    if not lines:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry")

    # Merge lines into a single geometry/network
    try:
        merged = linemerge(lines)
    except Exception:
        merged = unary_union(lines)

    # polygonize expects an iterable of linear geometries (MultiLineString / LineString)
    polys = list(polygonize(merged))
    if not polys:
        # try polygonize unary_union of lines
        try:
            polys = list(polygonize(unary_union(lines)))
        except Exception:
            polys = []

    if not polys:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry")

    gdf_polys = gpd.GeoDataFrame(geometry=polys, crs=gdf.crs)
    return gdf_polys.reset_index(drop=True)

def write_shapefile_and_zip(gdf, out_dir, base_name):
    os.makedirs(out_dir, exist_ok=True)
    shp_path = os.path.join(out_dir, f"{base_name}.shp")
    # If there are no properties (only geometry), ensure it's saved with a simple schema.
    gdf.to_file(shp_path, driver="ESRI Shapefile")
    base = os.path.splitext(shp_path)[0]
    files = glob.glob(f"{base}.*")
    zip_path = os.path.join(out_dir, f"{base_name}.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=os.path.basename(f))
    return zip_path

def make_folium_map(gdf_poly):
    # ensure GeoDataFrame is in EPSG:4326 for folium
    try:
        if gdf_poly.crs is None:
            gdf_4326 = gdf_poly.to_crs(epsg=4326)
        else:
            gdf_4326 = gdf_poly.to_crs(epsg=4326)
    except Exception:
        gdf_4326 = gdf_poly.copy()

    # compute bounds and center
    try:
        minx, miny, maxx, maxy = gdf_4326.total_bounds
        center_lat = (miny + maxy) / 2.0
        center_lon = (minx + maxx) / 2.0
    except Exception:
        # fallback to 0,0
        center_lat, center_lon = 0, 0
        miny, minx, maxy, maxx = -1, -1, 1, 1

    m = folium.Map(location=[center_lat, center_lon], zoom_start=10)
    folium.GeoJson(data=gdf_4326.__geo_interface__, name="polygons").add_to(m)

    # fit map to bounds (latlon order)
    try:
        m.fit_bounds([[miny, minx], [maxy, maxx]])
    except Exception:
        pass

    return m

# ---------- Streamlit UI ----------
st.set_page_config(page_title="KML/KMZ → Polygon Shapefile (ZIP)", layout="centered")
st.title("Upload KML/KMZ — auto convert polygons (auto polygonize lines) → Shapefile ZIP")

uploaded = st.file_uploader("Upload .kml or .kmz", type=["kml", "kmz"])

if uploaded is not None:
    tmp_root = tempfile.mkdtemp(prefix="kml2shp_")
    uid = uuid.uuid4().hex[:8]
    work_dir = os.path.join(tmp_root, uid)
    os.makedirs(work_dir, exist_ok=True)

    try:
        saved = os.path.join(work_dir, uploaded.name)
        with open(saved, "wb") as f:
            f.write(uploaded.getbuffer())
        st.info(f"Saved upload: {uploaded.name}")

        # locate KML
        if is_kmz(saved):
            exdir = os.path.join(work_dir, "extracted")
            os.makedirs(exdir, exist_ok=True)
            kml_path = extract_kmz(saved, exdir)
            if not kml_path:
                st.error("KMZ has no .kml inside.")
                raise RuntimeError("No KML in KMZ")
        else:
            kml_path = saved

        # read KML
        st.info("Reading KML...")
        try:
            gdf = read_kml(kml_path)
        except Exception as e:
            st.error(f"Failed to read KML: {e}")
            raise

        if gdf is None or len(gdf) == 0:
            st.warning("No features found in KML.")
            st.stop()

        st.write(f"Total features read: {len(gdf)}")

        # first: try to extract polygonal geometries
        st.info("Filtering polygon / multipolygon geometries...")
        gdf_poly = filter_polygons_gdf(gdf)

        # if none: try to polygonize lines
        if gdf_poly is None or len(gdf_poly) == 0:
            st.info("No polygons found — attempting to convert lines/polylines to polygons...")
            gdf_lines_polys = lines_to_polygons(gdf)
            if gdf_lines_polys is None or len(gdf_lines_polys) == 0:
                st.warning("Could not produce polygons from lines/polylines. Points/lines are ignored.")
                # offer original download
                with open(saved, "rb") as fh:
                    st.download_button("Download original upload", fh, file_name=uploaded.name)
                st.stop()
            else:
                # create GeoDataFrame ready for saving (no attributes)
                gdf_poly = gdf_lines_polys
                st.success(f"Polygonize produced {len(gdf_poly)} polygon(s).")

        # Ensure there's at least one polygon now
        if gdf_poly is None or len(gdf_poly) == 0:
            st.warning("No polygons available to save.")
            st.stop()

        # write shapefile and zip
        base = f"polygons_{int(time.time())}"
        outdir = os.path.join(work_dir, "out")
        zip_path = write_shapefile_and_zip(gdf_poly, outdir, base)

        # preview map and auto-zoom
        st.subheader("Preview")
        m = make_folium_map(gdf_poly)
        st_folium(m, width=700, height=450)

        # prepare download (read bytes to memory)
        with open(zip_path, "rb") as fh:
            data = fh.read()
        st.success("Conversion done. Download the ZIP below.")
        st.download_button("Download polygon shapefile (ZIP)", data, file_name=os.path.basename(zip_path))

    except Exception as e:
        st.exception(e)
    finally:
        try:
            shutil.rmtree(tmp_root)
        except Exception:
            pass
