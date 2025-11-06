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
from pathlib import Path
import sys
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

def find_kml_in_dir(d: str):
    for root, _, files in os.walk(d):
        for f in files:
            if f.lower().endswith(".kml"):
                return os.path.join(root, f)
    return None

def read_kml_with_geopandas(kml_path: str):
    """
    Try reading KML with geopandas. KML files may contain multiple layers.
    geopandas.read_file() can sometimes read KML directly.
    """
    try:
        gdf = gpd.read_file(kml_path)
        if len(gdf) == 0:
            # sometimes geopandas returns 0 features for KML; try specifying driver
            gdf = gpd.read_file(kml_path, driver="KML")
        return gdf
    except Exception as e:
        raise

def try_ogr2ogr_convert(input_kml: str, out_dir: str, out_basename: str):
    """
    Use ogr2ogr (CLI) to convert KML -> shapefile. This requires `ogr2ogr` available in PATH.
    Returns path to created shapefile (.shp) if successful.
    """
    out_shp = os.path.join(out_dir, f"{out_basename}.shp")
    try:
        cmd = ["ogr2ogr", "-f", "ESRI Shapefile", out_shp, input_kml]
        # Optionally add reprojection: e.g. ["-t_srs", "EPSG:4326"]
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        if os.path.exists(out_shp):
            return out_shp
    except Exception as e:
        # return None to fall back
        return None
    return None

def write_shapefile_from_gdf(gdf, out_dir: str, out_basename: str):
    out_path = os.path.join(out_dir, f"{out_basename}.shp")
    # ensure driver uses ESRI Shapefile
    gdf.to_file(out_path, driver="ESRI Shapefile")
    return out_path

def zip_shapefile_files(shp_basepath: str, zip_path: str):
    """
    shp_basepath: path/to/name.shp  (base name)
    Will include all files name.* in same dir.
    """
    base = os.path.splitext(shp_basepath)[0]
    dirpath = os.path.dirname(shp_basepath)
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
    """
    Return sha of file at repo:path if exists, else None.
    """
    headers = {"Authorization": f"token {token}"} if token else {}
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def github_upload_file(repo: str, path_in_repo: str, local_file_path: str, commit_message: str, branch="main", token=None):
    """
    Upload local_file_path to repo at path_in_repo (create or update).
    Returns the API response json.
    """
    if token is None:
        raise ValueError("GitHub token is required to upload.")
    with open(local_file_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    url = f"https://api.github.com/repos/{repo}/contents/{path_in_repo}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    payload = {
        "message": commit_message,
        "content": content_b64,
        "bran
