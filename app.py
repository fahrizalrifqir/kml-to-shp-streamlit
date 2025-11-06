# app.py
import streamlit as st
import geopandas as gpd
import zipfile
import tempfile
import shutil
import os
import io
import base64
import requests
import json
import uuid
import time
import glob
import subprocess

# ------------------- CONFIG -------------------
# GitHub config pulled from Streamlit secrets
# In .streamlit/secrets.toml or Streamlit Cloud secrets UI:
# [github]
# token = "ghp_...."
# repo = "username/repo"        # repo to store uploads, e.g. "user/kml-to-shp-storage"
# branch = "main"              # optional, default "main"

GITHUB_TOKEN = st.secrets.get("github", {}).get("token") if "github" in st.secrets else None
GITHUB_REPO = st.secrets.get("github", {}).get("repo") if "github" in st.secrets else None
GITHUB_BRANCH = st.secrets.get("github", {}).get("branch", "main") if "github" in st.secrets else "main"

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB limit per GitHub Contents API

# ------------------- HELPERS -------------------
def is_kmz(filename: str):
    return filename.lower().endswith(".kmz")

def is_kml(filename: str):
    return filename.lower().endswith(".kml")

def extract_kmz(kmz_path: str, dest_dir: str) -> str:
    """Extract KMZ (zip) and return path to first .kml found."""
    with zipfile.ZipFile(kmz_path, "r") as z:
        z.extractall(dest_dir)
    # find first kml
    for root, _, files in os.walk(dest_dir):
        for f in files:
            if f.lower().endswith(".kml"):
                return os.path.join(root, f)
    return None

def read_kml_with_geopandas(kml_path: str):
    """
    Try reading KML with geopandas. KML files may contain multiple layers.
    """
    try:
        try:
            gdf = gpd.read_file(kml_path)
            if len(gdf) == 0:
                gdf = gpd.read_file(kml_path, driver="KML")
        except Exception:
            gdf = gpd.read_file(kml_path, driver="KML")
        return gdf
    except Exception as e:
        raise

def try_ogr2ogr_convert(input_kml: str, out_dir: str, out_basename: str):
    """
    Use ogr2ogr (CLI) to convert KML -> shapefile. Requires ogr2ogr in PATH.
    Returns path to created shapefile (.shp) if successful.
    """
    out_shp = os.path.join(out_dir, f"{out_basename}.shp")
    try:
        cmd = ["ogr2ogr", "-f", "ESRI Shapefile", out_shp, input_kml]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        if os.path.exists(out_shp):
            return out_shp
    except Exception:
        return None
    return None

def write_shapefile_from_gdf(gdf, out_dir: str, out_basename: str):
    out_path = os.path.join(out_dir, f"{out_basename}.shp")
    # ensure driver uses ESRI Shapefile
    gdf.to_file(out_path, driver="ESRI Shapefile")
    return out_path

def zip_shapefile_files(shp_basepath: str, zip_path: str):
    base = os.path.splitext(shp_basepath)[0]
    files_to_zip = []
    for f in glob.glob(f"{base}.*"):
        files_to_zip.append(f)
    if not files_to_zip:
        raise FileNotFoundError("No shapefile parts found to zip.")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files_to_zip:
            z.write(f, arcname=os.path.basename(f))
    return zip_path

def file_under_limit(path: str, limit_bytes=MAX_UPLOAD_BYTES):
    return os.path.getsize(path) <= limit_bytes

# ------------------- GitHub upload functions -------------------
def github_get_file_sha(repo: str, path: str, branch="main", token=None):
    headers = {"Authorization": f"token {token}"} if token else {}
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def github_upload_file(repo: str, path_in_repo: str, local_file_path: str, commit_message: str, branch="main", token=None):
    if token is None:
        raise ValueError("GitHub token is required to upload.")
    with open(local_file_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch": branch
    }
    sha = github_get_file_sha(repo, path_in_repo, branch=branch, token=token)
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, data=json.dumps(payload))
    if r.status_code in (200, 201):
        return r.json()
    else:
        st.error(f"GitHub upload failed: {r.status_code} {r.text}")
        raise RuntimeError(f"GitHub upload failed: {r.status_code} {r.text}")

# ------------------- Streamlit UI -------------------
st.set_page_config(page_title="KML/KMZ → Shapefile (Polygon only)", layout="wide")
st.title("KML/KMZ → Shapefile (Polygon / MultiPolygon only)")

st.markdown("""
Upload a KML or KMZ file. Only Polygon and MultiPolygon features will be converted to a Shapefile (ZIP).
Points and polylines (LineString) will be ignored.
""")

uploaded = st.file_uploader("Upload .kml or .kmz", type=["kml", "kmz"], accept_multiple_files=False)

out_epsg = st.selectbox("Output CRS (optional)", ["Keep original", "EPSG:4326", "EPSG:3857"], index=0)
output_name_input = st.text_input("Output base name (without extension)", value=f"converted_polygons_{int(time.time())}")
commit_message_input = st.text_input("Git commit message (for GitHub upload)", value="Add converted polygon shapefile zip")

col1, col2 = st.columns([1, 2])
with col1:
    do_convert = st.button("Convert & Upload (Polygons only)")
with col2:
    st.write("GitHub repo from secrets:", "✅ set" if GITHUB_REPO and GITHUB_TOKEN else "⚠️ not set")

if do_convert:
    if uploaded is None:
        st.warning("Please upload a .kml or .kmz file.")
        st.stop()

    tmp_root = tempfile.mkdtemp(prefix="kml2shp_")
    uid = uuid.uuid4().hex[:8]
    work_dir = os.path.join(tmp_root, uid)
    os.makedirs(work_dir, exist_ok=True)

    try:
        # Save uploaded file
        uploaded_name = uploaded.name
        saved_upload_path = os.path.join(work_dir, uploaded_name)
        with open(saved_upload_path, "wb") as f:
            f.write(uploaded.getbuffer())
        st.info(f"Saved upload to `{saved_upload_path}`")

        # Extract/locate KML
        if is_kmz(saved_upload_path):
            st.info("Detected KMZ — extracting...")
            extract_dir = os.path.join(work_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            kml_path = extract_kmz(saved_upload_path, extract_dir)
            if not kml_path:
                st.error("No KML found inside KMZ.")
                raise RuntimeError("No KML found inside KMZ.")
            st.info(f"Found KML: {kml_path}")
        elif is_kml(saved_upload_path):
            kml_path = saved_upload_path
        else:
            st.error("Unsupported file type.")
            raise RuntimeError("Unsupported file type.")

        # Read with geopandas
        st.info("Reading KML with geopandas...")
        gdf = None
        try:
            gdf = read_kml_with_geopandas(kml_path)
            st.success(f"Read KML — total features: {len(gdf)}")
        except Exception as e:
            st.warning(f"geopandas read failed: {e}. Will try ogr2ogr fallback.")

        out_basename = output_name_input.strip() or f"converted_polygons_{int(time.time())}"
        shp_out_dir = os.path.join(work_dir, "shapefile_out")
        os.makedirs(shp_out_dir, exist_ok=True)
        shp_path = None

        # If geopandas succeeded, filter polygons
        if gdf is not None and len(gdf) > 0:
            # Keep only Polygon / MultiPolygon
            # Some geometries may be None — filter safely
            gdf = gdf[~gdf.geometry.isna()].copy()
            # geopandas stores geometry types in .geom_type or .geometry.type
            geom_types = gdf.geometry.type
            poly_mask = geom_types.isin(["Polygon", "MultiPolygon"])
            gdf_poly = gdf[poly_mask].copy()
            st.info(f"Polygon/MultiPolygon features found: {len(gdf_poly)}")
            if len(gdf_poly) == 0:
                st.warning("No Polygon or MultiPolygon features found in the uploaded file. Nothing to convert.")
                # Offer download of original uploaded file
                with open(saved_upload_path, "rb") as fh:
                    st.download_button("Download original upload", fh, file_name=uploaded_name)
                st.stop()

            # Optionally reproject
            if out_epsg != "Keep original":
                try:
                    epsg_code = int(out_epsg.split(":")[1])
                    gdf_poly = gdf_poly.to_crs(epsg=epsg_code)
                    st.info(f"Reprojected to {out_epsg}")
                except Exception as e:
                    st.warning(f"Failed to reproject: {e} — keeping original CRS.")

            # Write shapefile from filtered gdf
            st.info("Writing shapefile (only polygons) ...")
            shp_path = write_shapefile_from_gdf(gdf_poly, shp_out_dir, out_basename)
            st.success(f"Shapefile written: {shp_path}")

        else:
            # If geopandas failed to read, try ogr2ogr fallback to produce shapefile and then filter
            st.info("Attempting ogr2ogr fallback to produce shapefile...")
            ogr_shp = try_ogr2ogr_convert(kml_path, shp_out_dir, out_basename)
            if not ogr_shp:
                st.error("Conversion failed (geopandas and ogr2ogr). Ensure file is valid and GDAL/OGR is installed for the fallback.")
                raise RuntimeError("Conversion failed")
            # Read the shapefile created by ogr2ogr and filter polygons
            st.info("Reading ogr2ogr output and filtering polygons...")
            gdf_ogr = gpd.read_file(ogr_shp)
            gdf_ogr = gdf_ogr[~gdf_ogr.geometry.isna()].copy()
            geom_types = gdf_ogr.geometry.type
            poly_mask = geom_types.isin(["Polygon", "MultiPolygon"])
            gdf_poly = gdf_ogr[poly_mask].copy()
            st.info(f"Polygon/MultiPolygon features found: {len(gdf_poly)}")
            if len(gdf_poly) == 0:
                st.warning("No Polygon or MultiPolygon features found in the uploaded file. Nothing to convert.")
                with open(saved_upload_path, "rb") as fh:
                    st.download_button("Download original upload", fh, file_name=uploaded_name)
                st.stop()
            # Optionally reproject
            if out_epsg != "Keep original":
                try:
                    epsg_code = int(out_epsg.split(":")[1])
                    gdf_poly = gdf_poly.to_crs(epsg=epsg_code)
                    st.info(f"Reprojected to {out_epsg}")
                except Exception as e:
                    st.warning(f"Failed to reproject: {e} — keeping original CRS.")
            # Write validated polygon-only shapefile
            shp_path = write_shapefile_from_gdf(gdf_poly, shp_out_dir, out_basename)
            st.success(f"Shapefile written from ogr2ogr output: {shp_path}")

        # Zip shapefile files
        zip_name = f"{out_basename}.zip"
        zip_path = os.path.join(work_dir, zip_name)
        zip_shapefile_files(shp_path, zip_path)
        st.success(f"Created ZIP: {zip_path} (size: {os.path.getsize(zip_path)} bytes)")

        # Upload to GitHub or offer download
        if not (GITHUB_TOKEN and GITHUB_REPO):
            st.warning("GitHub token/repo not configured in Streamlit secrets. Skipping upload. You can download the ZIP below.")
            with open(zip_path, "rb") as fh:
                st.download_button("Download ZIP", fh, file_name=zip_name)
        else:
            if not file_under_limit(zip_path):
                st.error(f"ZIP exceeds GitHub API file size limit (>{MAX_UPLOAD_BYTES} bytes). Use external storage (S3) or Git LFS.")
                with open(zip_path, "rb") as fh:
                    st.download_button("Download ZIP", fh, file_name=zip_name)
                st.stop()

            path_in_repo = f"converted/{zip_name}"
            st.info(f"Uploading {zip_name} to GitHub repo {GITHUB_REPO} at path {path_in_repo} ...")
            try:
                resp = github_upload_file(
                    repo=GITHUB_REPO,
                    path_in_repo=path_in_repo,
                    local_file_path=zip_path,
                    commit_message=commit_message_input or f"Add {zip_name}",
                    branch=GITHUB_BRANCH,
                    token=GITHUB_TOKEN
                )
                download_url = resp.get("content", {}).get("download_url") or f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{path_in_repo}"
                st.success("Upload successful!")
                st.write("Download URL:")
                st.write(download_url)
                st.markdown(f"[Open download link]({download_url})")
            except Exception as e:
                st.error(f"Upload failed: {e}")
                with open(zip_path, "rb") as fh:
                    st.download_button("Download ZIP (local)", fh, file_name=zip_name)

    except Exception as e:
        st.exception(e)
    finally:
        try:
            shutil.rmtree(tmp_root)
        except Exception:
            pass
