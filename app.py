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
import zipfile
from pathlib import Path
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
from shapely.ops import unary_union

# ----------------- Helpers -----------------
def is_kmz(filename: str):
    return filename.lower().endswith(".kmz")

def is_kml(filename: str):
    return filename.lower().endswith(".kml")

def extract_kmz(kmz_path: str, dest_dir: str) -> str:
    """Extract KMZ (zip) and return path to first .kml found."""
    with zipfile.ZipFile(kmz_path, "r") as z:
        z.extractall(dest_dir)
    for root, _, files in os.walk(dest_dir):
        for f in files:
            if f.lower().endswith(".kml"):
                return os.path.join(root, f)
    return None

def read_kml_try_geopandas(kml_path: str):
    """
    Try reading KML with geopandas. Return a GeoDataFrame if success.
    """
    # geopandas sometimes needs driver="KML"
    try:
        gdf = gpd.read_file(kml_path)
        if gdf is None or len(gdf) == 0:
            gdf = gpd.read_file(kml_path, driver="KML")
        return gdf
    except Exception:
        # try explicitly driver
        gdf = gpd.read_file(kml_path, driver="KML")
        return gdf

def extract_polygons_from_geometry(geom):
    """
    Given a shapely geometry, return a list of polygon/multipolygon geometries extracted from it.
    - If geom is Polygon or MultiPolygon -> return [geom]
    - If geom is GeometryCollection -> return list of polygonal parts inside
    - Else -> return []
    """
    out = []
    if geom is None:
        return out
    gtype = geom.geom_type
    if gtype == "Polygon":
        out.append(geom)
    elif gtype == "MultiPolygon":
        # each part is polygonal but we can keep MultiPolygon as single geometry or break into polygons.
        out.append(geom)
    elif gtype == "GeometryCollection":
        for part in geom.geoms:
            if part.geom_type in ("Polygon", "MultiPolygon"):
                out.append(part)
    # else ignore Point / LineString / MultiLineString / etc.
    return out

def filter_polygons_from_gdf(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    From an input GeoDataFrame, return a GeoDataFrame containing only polygonal geometries.
    This will:
      - remove null geometries
      - extract polygon parts from GeometryCollection
      - keep Polygon and MultiPolygon
    """
    rows = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        poly_parts = extract_polygons_from_geometry(geom)
        if not poly_parts:
            # maybe the geometry is a Multi* containing polygons (already handled) or not polygonal -> skip
            continue
        # For each polygonal part, create a new row copying attributes (except geometry)
        for pg in poly_parts:
            new_row = row.copy()
            new_row.geometry = pg
            rows.append(new_row)
    if not rows:
        return gpd.GeoDataFrame(columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    gdf_poly = gpd.GeoDataFrame(rows, columns=list(gdf.columns), geometry="geometry", crs=gdf.crs)
    # Optionally, reset index
    gdf_poly = gdf_poly.reset_index(drop=True)
    return gdf_poly

def write_shapefile_and_zip(gdf: gpd.GeoDataFrame, out_dir: str, base_name: str):
    """
    Write GeoDataFrame to ESRI Shapefile under out_dir with base_name,
    then zip all files with that base_name and return zip path.
    """
    shp_path = os.path.join(out_dir, f"{base_name}.shp")
    # Ensure folder exists
    os.makedirs(out_dir, exist_ok=True)
    # Write shapefile
    gdf.to_file(shp_path, driver="ESRI Shapefile")
    # Find all files with same basename and zip them
    base = os.path.splitext(shp_path)[0]
    files = glob.glob(f"{base}.*")
    if not files:
        raise FileNotFoundError("No shapefile parts found after writing.")
    zip_name = f"{base_name}.zip"
    zip_path = os.path.join(out_dir, zip_name)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, arcname=os.path.basename(f))
    return zip_path

# --------------- Streamlit UI ---------------
st.set_page_config(page_title="KML/KMZ → Shapefile (Polygon only)", layout="centered")
st.title("KML/KMZ → Shapefile (Polygon only)")

st.write("Upload a KML or KMZ. The app will convert only Polygon / MultiPolygon (and polygon parts inside GeometryCollection) to a Shapefile ZIP. Points and polylines will be ignored.")

uploaded = st.file_uploader("Upload .kml or .kmz", type=["kml", "kmz"])

if uploaded is not None:
    # simple UI: show filename and Convert button
    st.write("Uploaded:", uploaded.name)
    if st.button("Convert to polygon-only shapefile (ZIP)"):
        tmp_root = tempfile.mkdtemp(prefix="kml2shp_")
        uid = uuid.uuid4().hex[:8]
        work_dir = os.path.join(tmp_root, uid)
        os.makedirs(work_dir, exist_ok=True)
        try:
            # save uploaded file
            saved_path = os.path.join(work_dir, uploaded.name)
            with open(saved_path, "wb") as f:
                f.write(uploaded.getbuffer())
            st.info(f"Saved upload to `{saved_path}`")

            # find kml
            if is_kmz(saved_path):
                st.info("Detected KMZ — extracting KML...")
                extract_dir = os.path.join(work_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                kml_path = extract_kmz(saved_path, extract_dir)
                if not kml_path:
                    st.error("No .kml file found inside the KMZ.")
                    raise RuntimeError("No KML found inside KMZ.")
            elif is_kml(saved_path):
                kml_path = saved_path
            else:
                st.error("Unsupported file type.")
                raise RuntimeError("Unsupported file type.")

            st.info("Reading KML...")
            try:
                gdf = read_kml_try_geopandas(kml_path)
            except Exception as e:
                st.error(f"Failed to read KML: {e}")
                raise

            if gdf is None or len(gdf) == 0:
                st.warning("No features found in KML.")
                st.stop()

            st.write(f"Total features read: {len(gdf)}")

            # Filter polygons (and extract polygon parts from GeometryCollection)
            st.info("Filtering polygon / multipolygon geometries...")
            gdf_poly = filter_polygons_from_gdf(gdf)
            st.write(f"Polygon/MultiPolygon features after filter: {len(gdf_poly)}")

            if gdf_poly is None or len(gdf_poly) == 0:
                st.warning("No Polygon or MultiPolygon features found. Nothing to convert.")
                # offer original file download
                with open(saved_path, "rb") as fh:
                    st.download_button("Download original upload", fh, file_name=uploaded.name)
                st.stop()

            # write shapefile and zip
            base_name = f"polygons_{int(time.time())}"
            out_dir = os.path.join(work_dir, "output")
            os.makedirs(out_dir, exist_ok=True)

            zip_path = write_shapefile_and_zip(gdf_poly, out_dir, base_name)
            st.success("Conversion complete.")

            # provide download
            with open(zip_path, "rb") as fh:
                st.download_button("Download polygon shapefile (ZIP)", fh, file_name=os.path.basename(zip_path))

            # show quick preview: first few rows and geometry types
            try:
                st.write("Preview (first 5 features):")
                st.write(gdf_poly.head()[ [c for c in gdf_poly.columns if c!='geometry'] + ["geometry"] ])
            except Exception:
                pass

        except Exception as e:
            st.exception(e)
        finally:
            # cleanup - keep the file available until Streamlit session ends (but we remove temp folder here to avoid disk filling)
            try:
                shutil.rmtree(tmp_root)
            except Exception:
                pass
