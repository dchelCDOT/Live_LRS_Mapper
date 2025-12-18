import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from shapely.ops import substring
import folium
from streamlit_folium import st_folium
import requests
import io
import os
from zipfile import ZipFile

# --- PAGE CONFIG ---
st.set_page_config(page_title="LRS Mapper Pro", page_icon="🛣️", layout="wide")

st.title("🛣️ LRS Mapper Pro")
st.markdown("""
**Instructions:**
1. Upload your CSV spreadsheet.
2. Map your columns.
3. Click **Run Analysis** to see the map and download results.
""")

# --- CONSTANTS ---
ROUTE_SERVICE_URL = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/arcgis/rest/services/Routes_gdb/FeatureServer/0"
CALC_CRS = "EPSG:3857" # Web Mercator (Meters)
MAP_CRS = "EPSG:4326"  # WGS84 (Lat/Long)

# --- UTILS ---
@st.cache_data
def get_layer_columns(service_url):
    params = {'where': '1=1', 'outFields': '*', 'f': 'json', 'resultRecordCount': 1}
    try:
        r = requests.get(f"{service_url}/query", params=params)
        data = r.json()
        if 'fields' in data: return [f['name'] for f in data['fields']]
        return []
    except: return []

@st.cache_data
def get_arcgis_features(service_url):
    all_features = []
    offset = 0
    with st.spinner("Fetching Route Network from ArcGIS..."):
        while True:
            params = {
                'where': '1=1', 'outFields': '*', 'f': 'geojson',
                'resultOffset': offset, 'resultRecordCount': 2000
            }
            try:
                r = requests.get(f"{service_url}/query", params=params)
                data = r.json()
                if 'features' not in data or not data['features']: break
                all_features.extend(data['features'])
                offset += len(data['features'])
                if 'exceededTransferLimit' not in data or not data['exceededTransferLimit']: break
            except: break
                
    fc = {"type": "FeatureCollection", "features": all_features}
    gdf = gpd.GeoDataFrame.from_features(fc['features'])
    gdf.set_crs(MAP_CRS, inplace=True)
    return gdf

# --- APP LOGIC ---

uploaded_file = st.file_uploader("Upload Spreadsheet (.csv)", type="csv")

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    csv_cols = list(df.columns)
    
    st.divider()
    st.subheader("⚙️ Configuration")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.info("Spreadsheet Mapping")
        csv_rid = st.selectbox("Spreadsheet Route ID", csv_cols, index=0 if csv_cols else None)
        bm_col = st.selectbox("Begin Milepost Column", csv_cols, index=1 if len(csv_cols)>1 else 0)
        em_col_opts = ['(None)'] + csv_cols
        em_col = st.selectbox("End Milepost Column", em_col_opts, index=0)
        
    with col2:
        st.warning("Settings")
        mode = st.radio("Feature Type", ["Point", "Line", "Both"], index=2)
        out_name = st.text_input("Output Filename", "LRS_Results")
    
    if st.button("🚀 Run Analysis", type="primary"):
        # 1. Load Routes
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None:
            st.error("Failed to load routes from ArcGIS.")
            st.stop()
            
        routes = raw_routes.to_crs(CALC_CRS)
        
        # 2. Auto-Detect GIS Route Column
        gis_rid = None
        possible_names = ['ROUTE', 'Route', 'route', 'RouteID', 'Route_ID', 'RteID']
        for name in possible_names:
            if name in routes.columns:
                gis_rid = name
                break
        
        if not gis_rid:
            for c in routes.columns:
                if c.upper() == 'ROUTE':
                    gis_rid = c
                    break
                    
        if not gis_rid:
            gis_rid = routes.columns[0]
        
        # 3. Process
        routes[gis_rid] = routes[gis_rid].astype(str)
        df[csv_rid] = df[csv_rid].astype(str)
        
        valid_pts, valid_lns, errors = [], [], []
        unit_factor = 1609.34 # Hardcoded Miles
        
        progress_bar = st.progress(0)
        total_rows = len(df)
        
        for idx, row in df.iterrows():
            if idx % max(1, int(total_rows/10)) == 0:
                progress_bar.progress(idx / total_rows)

            # --- SINGLE TRY BLOCK FOR STABILITY ---
            try:
                rid = row[csv_rid]
                match = routes[routes[gis_rid] == rid]
                
                if match.empty:
                    raise ValueError(f"Route '{rid}' Not Found")
                    
                geom_meters = match.iloc[0].geometry
                
                # Parse Begin Measure
                try:
                    bm_val = float(row[bm_col])
                except:
                    raise ValueError(f"Invalid Begin Measure: {row[bm_col]}")
                    
                bm_meters = bm_val * unit_factor
                
                # Determine Feature Type
                is_point = False
                if mode.lower() == 'point': is_point = True
                elif mode.lower() == 'line': is_point = False
                else: # Both
                    if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
                
                # --- GEOMETRY GENERATION ---
                if is_point:
                    pt_geom = geom_meters.interpolate(bm_meters)
                    res = row.copy()
                    res['geometry'] = pt_geom
                    valid_pts.append(res)
                else:
                    # Line Logic
                    try:
                        em_val = float(row[em_col])
                    except:
                         raise ValueError(f"Invalid End Measure: {row.get(em_col)}")
                         
                    em_meters = em_val * unit_factor
                    
                    if bm_meters >= em_meters:
                        if bm_meters == em_meters: 
                            raise ValueError("End == Begin (Use Point mode)")
                        else:
                            raise ValueError("End Milepost is less than Begin Milepost")
                    
                    ln_geom = substring(geom_meters, bm_meters, em_meters)
                    
                    if ln_geom.is_empty:
                        raise ValueError("Resulting geometry is empty")
                    elif ln_geom.geom_type in ['Point', 'MultiPoint']:
                        raise ValueError("Geometry collapsed to Point (length too short)")
                    else:
                        res = row.copy()
                        res['geometry'] = ln_geom
                        valid_lns.append(res)

            except Exception as e:
                # Catch ANY error from the block above
                errors.append({**row, "Error": str(e)})
                    
        progress_bar.progress(100)
        
        # 4. Results & Map
        st.success(f"Processing Complete! Points: {len(valid_pts)} | Lines: {len(valid_lns)} | Errors: {len(errors)}")
        
        # Map Prep (Centered on Colorado)
        m = folium.Map(location=[39.0, -105.5], zoom_start=7)
        
        def add_layer(data, name, color):
            if not data: return
            gdf = gpd.GeoDataFrame(data, crs=CALC_CRS).to_crs(MAP_CRS)
            cols = [c for c in gdf.columns if c != 'geometry']
            folium.GeoJson(
                gdf, name=name,
                style_function=lambda x: {'color': color, 'weight': 5},
                popup=folium.GeoJsonPopup(fields=cols)
            ).add_to(m)
            
        add_layer(valid_lns, "Mapped Lines", "blue")
        add_layer(valid_pts, "Mapped Points", "red")
        folium.LayerControl().add_to(m)
        
        st_folium(m, width=1000, height=600)
        
        # 5. Zip Download
        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, 'w') as zipf:
            if errors:
                err_csv = pd.DataFrame(errors).to_csv(index=False)
                zipf.writestr("Error_Report.csv", err_csv)
            
            def write_shp_to_zip(data, name):
                if not data: return
                tmp_gdf = gpd.GeoDataFrame(data, crs=CALC_CRS).to_crs(MAP_CRS)
                tmp_path = f"/tmp/{name}.shp"
                tmp_gdf.to_file(tmp_path)
                
                base_dir = "/tmp"
                for f in os.listdir(base_dir):
                    if f.startswith(name): 
                        zipf.write(os.path.join(base_dir, f), f)
                        os.remove(os.path.join(base_dir, f)) 
            
            write_shp_to_zip(valid_lns, f"{out_name}_Lines")
            write_shp_to_zip(valid_pts, f"{out_name}_Points")
            
        st.download_button(
            label="📦 Download ZIP Result",
            data=zip_buffer.getvalue(),
            file_name=f"{out_name}.zip",
            mime="application/zip"
        )
