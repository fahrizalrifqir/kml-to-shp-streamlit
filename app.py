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
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="KML/KMZ → Polygon Shapefile (ZIP)", layout="centered")
st.title("Upload KML/KMZ — auto convert polygons → Shapefile (ZIP)")

# ---------- helpers ----------
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
        # return empty GeoDataFrame with same columns
        return gpd.GeoDataFrame(columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    gdf_poly = gpd.GeoDataFrame(rows, columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    return gdf_poly.reset_index(drop=True)

def write_shapefile_and_zip(gdf, out_dir, base_name):
    os.makedirs(out_dir, exist_ok=True)
    shp_path = os.path.join(out_dir, f"{base_name}.shp")
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
    # center map on union centroid
    try:
        centroid = gdf_4326.unary_union.centroid
        lat, lon = centroid.y, centroid.x
    except Exception:
        # fallback center
        lat, lon = 0, 0
    m = folium.Map(location=[lat, lon], zoom_start=6)
    folium.GeoJson(data=gdf_4326.__geo_interface__, name="polygons").add_to(m)
    return m

# ---------- UI: single uploader, auto convert ----------
uploaded = st.file_uploader("Upload .kml or .kmz", type=["kml", "kmz"])

if uploaded is not None:
    # Immediately process
    tmp_root = tempfile.mkdtemp(prefix="kml2shp_")
    uid = uuid.uuid4().hex[:8]
    work_dir = os.path.join(tmp_root, uid)
    os.makedirs(work_dir, exist_ok=True)
    try:
        saved = os.path.join(work_dir, uploaded.name)
        with open(saved, "wb") as f:
            f.write(uploaded.getbuffer())
        st.info(f"Saved upload: {uploaded.name}")

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
        gdf = read_kml(kml_path)
        if gdf is None or len(gdf) == 0:
            st.warning("No features found in KML.")
            st.stop()

        # filter polygonal geometries
        st.info("Filtering polygons...")
        gdf_poly = filter_polygons_gdf(gdf)

        if gdf_poly is None or len(gdf_poly) == 0:
            st.warning("No Polygon/MultiPolygon features found. Points/lines ignored.")
            # offer download original
            with open(saved, "rb") as fh:
                st.download_button("Download original upload", fh, file_name=uploaded.name)
            st.stop()

        # write shapefile and zip
        base = f"polygons_{int(time.time())}"
        outdir = os.path.join(work_dir, "out")
        zip_path = write_shapefile_and_zip(gdf_poly, outdir, base)

        # show folium map preview
        st.subheader("Preview on map")
        m = make_folium_map(gdf_poly)
        st_folium(m, width=700, height=450)

        # provide download button (read bytes)
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
