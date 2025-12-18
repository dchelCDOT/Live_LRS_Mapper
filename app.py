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

# --- PAGE CONFIG & CDOT BRANDING ---
st.set_page_config(
    page_title="CDOT LRS Mapper Pro",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Official CDOT Colors and Custom CSS
CDOT_BLUE = "#004899"
CDOT_GREEN = "#78BE20"
CDOT_LOGO_URL = "https://www.codot.gov/++theme++codot.theme/images/cdot-logo.png"

st.markdown(f"""
    <style>
        /* Main headers */
        h1, h2, h3 {{
            color: {CDOT_BLUE} !important;
        }}
        /* Custom Banner Style */
        .cdot-banner {{
            background-color: {CDOT_BLUE};
            padding: 20px;
            border-radius: 10px;
            color: white;
            display: flex;
            align-items: center;
            margin-bottom: 20px;
        }}
        .cdot-banner img {{
            height: 60px;
            margin-right: 20px;
        }}
        .cdot-banner h1 {{
            color: white !important;
            margin: 0;
            padding: 0;
            font-size: 2.5rem;
        }}
        /* Primary Button Override */
        div.stButton > button:first-child {{
            background-color: {CDOT_GREEN};
            color: white;
            border: none;
            font-weight: bold;
        }}
        div.stButton > button:first-child:hover {{
            background-color: {CDOT_BLUE};
            border: 2px solid {CDOT_GREEN};
        }}
        /* Info box styling */
        .stAlert {{
            border-left-color: {CDOT_BLUE} !important;
        }}
    </style>
""", unsafe_allow_html=True)

# --- APP HEADER ---
st.markdown(f"""
    <div class="cdot-banner">
        <img src="{CDOT_LOGO_URL}" alt="CDOT Logo">
        <h1>LRS Mapper Pro</h1>
    </div>
""", unsafe_allow_html=True)

# --- INSTRUCTIONS & ABOUT ---
with st.expander("ℹ️ About this Tool & How to Use (Click to Expand)", expanded=True):
    col_about, col_how = st.columns(2)
    with col_about:
        st.subheader("What is this tool?")
        st.markdown("""
        This application is a **Linear Referencing System (LRS) Mapper**. 
        
        It is designed to take tabular data (like spreadsheets containing asset locations, project limits, or maintenance records) and visualize it spatially on a map without requiring complex GIS software like ArcGIS Pro.
        
        **What it's for:**
        * Mapping data that only has Route IDs and Mileposts.
        * Quickly validating location data quality.
        * Generating Shapefiles for use in other systems.
        """)
    
    with col_how:
        st.subheader("How to use it")
        st.markdown("""
        1.  **Upload Data:** Drag and drop your `.csv` spreadsheet below.
        2.  **Configure Mapping:** Tell the tool which columns in your spreadsheet contain the **Route ID**, **Begin Milepost**, and **End Milepost**.
        3.  **Select Type:** Choose if you are mapping Points, Lines, or both.
        4.  **Run Analysis:** Click the green button. The tool will fetch the official CDOT road network and perform the mapping math.
        5.  **Review & Download:** Interactively check the map results and download a ZIP file containing Shapefiles and an error report.
        """)

st.divider()

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
    with st.spinner("Fetching Official CDOT Route Network..."):
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

# --- SESSION STATE SETUP ---
if 'results' not in st.session_state:
    st.session_state['results'] = None

# --- APP LOGIC ---
st.subheader("1. Data Upload")
uploaded_file = st.file_uploader("Upload your spreadsheet here (.csv format)", type="csv")

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    csv_cols = list(df.columns)
    
    st.divider()
    st.subheader("2. Configuration")
    st.markdown("Map your spreadsheet columns to the required inputs.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown(f"<h4 style='color:{CDOT_BLUE}'>Spreadsheet Columns</h4>", unsafe_allow_html=True)
        csv_rid = st.selectbox("Which column holds the Route ID?", csv_cols, index=0 if csv_cols else None)
        bm_col = st.selectbox("Which column holds the Begin Milepost?", csv_cols, index=1 if len(csv_cols)>1 else 0)
        em_col_opts = ['(None)'] + csv_cols
        em_col = st.selectbox("Which column holds the End Milepost? (Optional for points)", em_col_opts, index=0)
        
    with col2:
        st.markdown(f"<h4 style='color:{CDOT_BLUE}'>Analysis Settings</h4>", unsafe_allow_html=True)
        mode = st.radio("What feature type are you mapping?", ["Point", "Line", "Both"], index=2)
        out_name = st.text_input("Desired Output Filename (no extension needed)", "CDOT_LRS_Results")
    
    st.divider()
    # --- RUN BUTTON ---
    if st.button("🚀 Run LRS Analysis", type="primary"):
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
        unit_factor = 1609.34 # Hardcoded Miles to Meters
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        total_rows = len(df)
        
        for idx, row in df.iterrows():
            if idx % max(1, int(total_rows/10)) == 0:
                progress = idx / total_rows
                progress_bar.progress(progress)
                status_text.text(f"Processing row {idx+1} of {total_rows}...")

            try:
                rid = row[csv_rid]
                match = routes[routes[gis_rid] == rid]
                
                if match.empty:
                    raise ValueError(f"Route ID '{rid}' Not Found in CDOT Network")
                    
                geom_meters = match.iloc[0].geometry
                
                try: bm_val = float(row[bm_col])
                except: raise ValueError(f"Invalid Begin Measure value: {row[bm_col]}")
                    
                bm_meters = bm_val * unit_factor
                
                is_point = False
                if mode.lower() == 'point': is_point = True
                elif mode.lower() == 'line': is_point = False
                else: 
                    if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
                
                if is_point:
                    pt_geom = geom_meters.interpolate(bm_meters)
                    res = row.copy()
                    res['geometry'] = pt_geom
                    valid_pts.append(res)
                else:
                    try: em_val = float(row[em_col])
                    except: raise ValueError(f"Invalid End Measure value: {row.get(em_col)}")
                         
                    em_meters = em_val * unit_factor
                    
                    if bm_meters >= em_meters:
                        if bm_meters == em_meters: raise ValueError("Begin MP equals End MP (Please use Point mode for this feature)")
                        else: raise ValueError(f"End MP ({em_val}) is less than Begin MP ({bm_val})")
                    
                    ln_geom = substring(geom_meters, bm_meters, em_meters)
                    
                    if ln_geom.is_empty: raise ValueError("Resulting geometry is empty (measures may be outside route limits)")
                    elif ln_geom.geom_type in ['Point', 'MultiPoint']: raise ValueError("Geometry collapsed to Point due to precision (length too short)")
                    else:
                        res = row.copy()
                        res['geometry'] = ln_geom
                        valid_lns.append(res)

            except Exception as e:
                errors.append({**row, "Error_Message": str(e)})
                    
        progress_bar.progress(100)
        status_text.text("Finalizing results...")
        
        # SAVE TO SESSION STATE
        st.session_state['results'] = {
            'pts': valid_pts,
            'lns': valid_lns,
            'errors': errors,
            'out_name': out_name
        }

# --- RENDER RESULTS (Persistent) ---
if st.session_state['results']:
    res = st.session_state['results']
    st.divider()
    st.subheader("3. Results & Download")
    
    # Summary Metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("Mapped Points", len(res['pts']))
    m2.metric("Mapped Lines", len(res['lns']))
    m3.metric("Rows with Errors", len(res['errors']), delta_color="inverse")

    if res['errors']:
        st.warning(f"⚠️ {len(res['errors'])} rows could not be mapped. Please download the result ZIP and check the 'Error_Report.csv' file for details on how to fix your data.")

    # MAP
    st.markdown("##### Interactive Preview Map (Center: Colorado)")
    # Center roughly on Colorado
    m = folium.Map(location=[39.113, -105.358], zoom_start=7)
    
    def add_layer(data, name, color):
        if not data: return
        gdf = gpd.GeoDataFrame(data, crs=CALC_CRS).to_crs(MAP_CRS)
        cols = [c for c in gdf.columns if c != 'geometry']
        folium.GeoJson(
            gdf, name=name,
            style_function=lambda x: {'color': color, 'weight': 5},
            popup=folium.GeoJsonPopup(fields=cols)
        ).add_to(m)
        
    add_layer(res['lns'], "Mapped Lines", CDOT_BLUE)
    add_layer(res['pts'], "Mapped Points", CDOT_GREEN)
    folium.LayerControl().add_to(m)
    
    st_folium(m, width=1000, height=600)
    
    # DOWNLOAD
    st.divider()
    st.subheader("Download Files")
    st.write("Click below to download a ZIP file containing Shapefiles of your mapped features and a CSV report of any errors.")
    
    zip_buffer = io.BytesIO()
    with ZipFile(zip_buffer, 'w') as zipf:
        if res['errors']:
            err_csv = pd.DataFrame(res['errors']).to_csv(index=False)
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
                    try: os.remove(os.path.join(base_dir, f)) 
                    except: pass
        
        write_shp_to_zip(res['lns'], f"{res['out_name']}_Lines")
        write_shp_to_zip(res['pts'], f"{res['out_name']}_Points")
        
    st.download_button(
        label=f"📦 Download Final Results ({res['out_name']}.zip)",
        data=zip_buffer.getvalue(),
        file_name=f"{res['out_name']}.zip",
        mime="application/zip",
        type="primary"
    )
