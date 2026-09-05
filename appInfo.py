import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64
import json

# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

FILE_GIRO_PERSISTENTE = "giro_salvato.json"
FILE_DB_PERSISTENTE = "database_salvato.xlsx"
FILE_UTENTI_PERSISTENTE = "utenti_salvati.json"

# Inizializzazione prioritaria delle variabili di sessione
if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "welcome"

if 'autenticato' not in st.session_state:
    st.session_state.autenticato = False

if 'is_admin' not in st.session_state:
    st.session_state.is_admin = False

if 'db_clienti' not in st.session_state:
    st.session_state.db_clienti = pd.DataFrame()

if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

if 'clienti_selezionati_m' not in st.session_state:
    st.session_state.clienti_selezionati_m = []

if 'vista_pulita' not in st.session_state:
    st.session_state.vista_pulita = False

# Gestione utenti persistenti
def carica_utenti():
    utenti_default = {"admin": "vango2026", "autista": "consegne2026"}
    if os.path.exists(FILE_UTENTI_PERSISTENTE):
        try:
            with open(FILE_UTENTI_PERSISTENTE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return utenti_default

def salva_utenti(dict_utenti):
    try:
        with open(FILE_UTENTI_PERSISTENTE, "w") as f:
            json.dump(dict_utenti, f, indent=4)
    except Exception as e:
        st.error(f"Errore nel salvataggio degli utenti: {e}")

if 'utenti_sistema' not in st.session_state:
    st.session_state.utenti_sistema = carica_utenti()

# Gestione navigazione tramite parametri URL
if "nav" in st.query_params and st.query_params["nav"] == "login":
    st.session_state.pagina_attiva = "login"
    st.query_params.clear()

# CSS Avanzato - Forzatura Dark Mode & Fix UI Mobile a Schermo Intero
st.markdown("""
<style>
    .stApp, body, html {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }

    header {visibility: hidden;}
    .stMainBlockContainer {
        padding: 0rem !important;
        max-width: 100% !important;
    }
    .block-container {
        padding-top: 0.5rem !important;
        padding-bottom: 1rem !important;
        max-width: 100% !important;
    }

    .logo-container {
        display: flex;
        justify-content: center;
        align-items: center;
        margin-bottom: 10px;
    }
    .logo-container img {
        width: 140px !important;
        max-width: 100%;
        height: auto;
    }

    div[data-testid="stHorizontalBlock"] {
        gap: 0.5rem !important;
        margin-bottom: -0.5rem !important;
    }
    div[data-testid="column"] {
        margin-bottom: 0px !important;
    }

    [data-testid="stMetricLabel"] {
        color: #CCCCCC !important;
        font-size: 14px !important;
        font-weight: 600 !important;
    }
    [data-testid="stMetricValue"] {
        color: #FFFFFF !important;
        font-size: 28px !important;
        font-weight: bold !important;
    }

    div[data-testid="stButton"] > button {
        background-color: #1E293B !important;
        color: #FFFFFF !important;
        border: 1px solid #475569 !important;
        border-radius: 8px !important;
        font-weight: bold !important;
    }

    .btn-active div[data-testid="stButton"] > button {
        background-color: #2563EB !important;
        color: #FFFFFF !important;
        border: 2px solid #60A5FA !important;
        height: 46px !important;
        font-size: 14px !important;
    }

    .btn-inactive div[data-testid="stButton"] > button {
        background-color: #1E293B !important;
        color: #94A3B8 !important;
        border: 1px solid #334155 !important;
        height: 46px !important;
        font-size: 14px !important;
    }

    div[data-baseweb="select"] {
        background-color: #1E293B !important;
        border-radius: 8px !important;
    }

    div[data-baseweb="select"] > div {
        background-color: #1E293B !important;
        color: #FFFFFF !important;
        border: 1px solid #3B82F6 !important;
        border-radius: 8px !important;
    }

    .stop-card {
        background-color: #1E1E1E;
        border-left: 5px solid #2563EB;
        padding: 12px 14px;
        border-radius: 10px;
        margin-top: 10px;
        border: 1px solid #334155;
    }
    .stop-title { font-size: 17px; font-weight: bold; color: #FFFFFF; margin-bottom: 4px; }
    .stop-address { font-size: 14px; color: #E2E8F0; margin-bottom: 6px; }
    .stop-meta { font-size: 13px; color: #60A5FA; font-weight: 600; }

    .clean-card {
        background-color: #1E1E1E;
        border: 1px solid #334155;
        border-radius: 14px;
        padding: 12px 16px;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 16px;
    }
    .clean-badge {
        background-color: #DBEAFE;
        color: #1D4ED8;
        width: 36px;
        height: 36px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        font-size: 16px;
        flex-shrink: 0;
    }
    .clean-content { flex-grow: 1; }
    .clean-title { font-size: 16px; font-weight: bold; color: #FFFFFF; margin-bottom: 2px; }
    .clean-subtitle { font-size: 13px; color: #94A3B8; }
</style>
""", unsafe_allow_html=True)

# Funzioni di utilità blindate per la lettura del DB
def pulisci_orario(valore):
    if pd.isna(valore):
        return ""
    val_str = str(valore).strip()
    if 'days' in val_str:
        val_str = val_str.split()[-1]
    if ' ' in val_str:
        val_str = val_str.split()[-1]
    if len(val_str) >= 5:
        return val_str[:5]
    return val_str

def elabora_dataframe_db(df):
    df.columns = df.columns.str.strip().str.upper()
    
    if 'POSIZIONE' in df.columns:
        df['POSIZIONE'] = pd.to_numeric(df['POSIZIONE'], errors='coerce').fillna(0).astype(int)
    else:
        df['POSIZIONE'] = range(1, len(df) + 1)
        
    if 'QTA_DEFAULT' in df.columns:
        df['QTA_DEFAULT'] = pd.to_numeric(df['QTA_DEFAULT'], errors='coerce').fillna(0).astype(int)
    else:
        df['QTA_DEFAULT'] = 0

    for col in ['ZONA', 'CLIENTE', 'COMUNE', 'VIA']:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
        else:
            df[col] = ""

    if 'ORA' in df.columns:
        df['ORA'] = df['ORA'].apply(pulisci_orario)
    else:
        df['ORA'] = ""
        
    return df.sort_values(by="POSIZIONE").reset_index(drop=True)

def salva_db_su_disco(df):
    try:
        df.to_excel(FILE_DB_PERSISTENTE, index=False)
    except Exception as e:
        st.error(f"Errore nel salvataggio del database: {e}")

def carica_db_predefinito():
    if os.path.exists(FILE_DB_PERSISTENTE):
        try:
            df = pd.read_excel(FILE_DB_PERSISTENTE)
            return elabora_dataframe_db(df)
        except Exception:
            pass

    nomi_file_possibili = ["database.xlsx", "database.csv", "database"]
    for file_path in nomi_file_possibili:
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path) if file_path.endswith('.csv') else pd.read_excel(file_path)
                df_elaborato = elabora_dataframe_db(df)
                salva_db_su_disco(df_elaborato)
                return df_elaborato
            except Exception:
                pass
    return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

def salva_giro_su_disco(df):
    try:
        df.to_json(FILE_GIRO_PERSISTENTE, orient="records", date_format="iso")
    except Exception as e:
        st.error(f"Errore nel salvataggio del giro: {e}")

def carica_giro_da_disco():
    if os.path.exists(FILE_GIRO_PERSISTENTE):
        try:
            df = pd.read_json(FILE_GIRO_PERSISTENTE, orient="records")
            if not df.empty:
                df['POSIZIONE'] = range(1, len(df) + 1)
                return df
        except Exception:
            pass
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Caricamento effettivo dei dati se vuoti
if st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_db_predefinito()

if st.session_state.giro_corrente.empty:
    st.session_state.giro_corrente = carica_giro_da_disco()

# ==========================================
# SCHERMATA 0: WELCOME / HOME PAGE GRAFICA A TUTTO SCHERMO
# ==========================================
if st.session_state.pagina_attiva == "welcome":
    img_path = "vango_splash.png"
    if os.path.exists(img_path):
        with open(img_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode()
        
        st.markdown(f"""
        <style>
            .hero-fullscreen {{
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background-image: url("data:image/png;base64,{encoded_string}");
                background-size: cover;
                background-position: left center;
                background-repeat: no-repeat;
                z-index: 99999;
                display: flex;
                justify-content: center;
                align-items: flex-end;
            }}
            .hero-btn-overlay {{
                position: absolute;
                bottom: 6%;
                left: 50%;
                transform: translateX(-50%);
                background: rgba(18, 18, 18, 0.4) !important;
                backdrop-filter: blur(8px);
                -webkit-backdrop-filter: blur(8px);
                color: #FFFFFF !important;
                padding: 14px 20px;
                border-radius: 30px;
                font-weight: bold;
                text-decoration: none !important;
                text-align: center;
                width: 85%;
                max-width: 400px;
                font-size: 16px;
                border: 2px solid rgba(96, 165, 250, 0.8) !important;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                z-index: 100000;
                transition: all 0.3s ease;
            }}
            .hero-btn-overlay:hover {{
                background: rgba(37, 99, 235, 0.7) !important;
                border-color: #60A5FA !important;
                color: #FFFFFF !important;
            }}
        </style>

        <div class="hero-fullscreen">
            <a href="?nav=login" target="_self" class="hero-btn-overlay">ENTRA IN VanGo</a>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.warning("⚠️ Immagine 'vango_splash.png' non trovata nella cartella.")
        if st.button("ENTRA IN VanGo", use_container_width=True, type="primary"):
            st.session_state.pagina_attiva = "login"
            st.rerun()

# ==========================================
# SCHERMATA DI LOGIN
# ==========================================
elif st.session_state.pagina_attiva == "login" or not st.session_state.autenticato:
    st.markdown("<div style='margin-top: 40px;'></div>", unsafe_allow_html=True)
    
    icon_path = "icovg.png"
    if os.path.exists(icon_path):
        with open(icon_path, "rb") as icon_file:
            encoded_icon = base64.b64encode(icon_file.read()).decode()
        st.markdown(f'''
            <div class="logo-container">
                <img src="data:image/png;base64,{encoded_icon}" alt="VanGo Logo">
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown("<h1 style='text-align: center; color: #FFFFFF; font-size: 26px;'>🚐 ACCESSO VANGO</h1>", unsafe_allow_html=True)

    st.markdown("<p style='text-align: center; color: #94A3B8; font-size: 14px; margin-bottom: 30px;'>Inserisci le credenziali per accedere al sistema</p>", unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 2, 1])
    with col_l2:
        with st.form("form_login"):
            username_input = st.text_input("Utente")
            password_input = st.text_input("Password", type="password")
            
            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            submit_login = st.form_submit_button("ACCEDI", use_container_width=True, type="primary")

            if submit_login:
                utenti_validi = st.session_state.utenti_sistema
                username_input = username_input.strip()

                if username_input in utenti_validi and utenti_validi[username_input] == password_input:
                    st.session_state.autenticato = True
                    st.session_state.utente_corrente = username_input
                    st.session_state.is_admin = (username_input.lower() == "admin")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()
                else:
                    st.error("❌ Utente o password errati.")

        if st.button("⬅️ Torna alla Home", use_container_width=True):
            st.session_state.pagina_attiva = "welcome"
            st.rerun()

# ==========================================
# APPLICAZIONE PRINCIPALE (ACCESSO CONSENTITO)
# ==========================================
else:
    icon_path = "icovg.png"
    if os.path.exists(icon_path):
        with open(icon_path, "rb") as icon_file:
            encoded_icon = base64.b64encode(icon_file.read()).decode()
        st.markdown(f'''
            <div class="logo-container">
                <img src="data:image/png;base64,{encoded_icon}" alt="VanGo Logo">
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown("<h1 style='text-align: center; color: #FFFFFF; font-size: 22px; margin-bottom: 5px; margin-top: 0px;'>🚐 VANGO</h1>", unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

    # Menu di navigazione dinamico
    if st.session_state.is_admin:
        col_sw1, col_sw2, col_sw3 = st.columns(3)
    else:
        col_sw1, col_sw2 = st.columns(2)

    with col_sw1:
        css_class = "btn-active" if st.session_state.pagina_attiva == "giro" else "btn-inactive"
        st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
        if st.button("📍 GIRO", use_container_width=True, key="btn_giro"):
            st.session_state.pagina_attiva = "giro"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_sw2:
        css_class = "btn-active" if st.session_state.pagina_attiva == "db" else "btn-inactive"
        st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
        if st.button("📁 CLIENTI", use_container_width=True, key="btn_db"):
            st.session_state.pagina_attiva = "db"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state.is_admin:
        with col_sw3:
            css_class = "btn-active" if st.session_state.pagina_attiva == "utenti" else "btn-inactive"
            st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
            if st.button("🔑 UTENTI", use_container_width=True, key="btn_utenti"):
                st.session_state.pagina_attiva = "utenti"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    col_act1, col_act2 = st.columns(2)

    with col_act1:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("🔄 INVERTI SEQUENZA", use_container_width=True, key="btn_inverti"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                salva_giro_su_disco(st.session_state.giro_corrente)
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_act2:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("🗑️ SVUOTA GIRO", use_container_width=True, key="btn_svuota"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                salva_giro_su_disco(st.session_state.giro_corrente)
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    # ==========================================
    # SCHERMATA 1: GIRO CONSEGNE
    # ==========================================
    if st.session_state.pagina_attiva == "giro":
        tot_clienti = len(st.session_state.giro_corrente)
        tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum()) if not st.session_state.giro_corrente.empty else 0
        tot_comuni = int(st.session_state.giro_corrente['COMUNE'].nunique()) if not st.session_state.giro_corrente.empty else 0

        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Fermate Totali", f"{tot_clienti}")
        col_m2.metric("Pezzi Totali", f"{tot_qta}")
        col_m3.metric("Comuni", f"{tot_comuni}")

        st.markdown("---")

        if not st.session_state.giro_corrente.empty:
            label_btn_vista = "👁️ TORNA ALLA VISTA OPERATIVA" if st.session_state.vista_pulita else "📋 VISTA RIEPILOGO PULITA"
            if st.button(label_btn_vista, use_container_width=True):
                st.session_state.vista_pulita = not st.session_state.vista_pulita
                st.rerun()
            st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

        if not st.session_state.giro_corrente.empty:
            st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
            
            addresses = [f"{r['VIA']}, {r['COMUNE']}" for _, r in st.session_state.giro_corrente.iterrows()]
            if len(addresses) == 1:
                maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(addresses[0])}"
            else:
                origin = urllib.parse.quote(addresses[0])
                destination = urllib.parse.quote(addresses[-1])
                waypoints = "|".join([urllib.parse.quote(a) for a in addresses[1:-1]])
                maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"

            if st.session_state.vista_pulita:
                st.markdown(f"<p style='color: #94A3B8; font-size: 14px; margin-bottom: 15px;'>{tot_clienti} indirizzi trovati nel giro.</p>", unsafe_allow_html=True)
                
                for idx in range(tot_clienti):
                    row = st.session_state.giro_corrente.iloc[idx]
                    st.markdown(f"""
                    <div class="clean-card">
                        <div class="clean-badge">{idx + 1}</div>
                        <div class="clean-content">
                            <div class="clean-title">{row['VIA']}</div>
                            <div class="clean-subtitle">{row['COMUNE']} — Cliente: {row['CLIENTE']} (🕒 {row['ORA']} | 📦 {row['Q.ta']} pz)</div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                st.markdown("---")
                st.markdown(f'''
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                ''', unsafe_allow_html=True)
            else:
                for idx in range(tot_clienti):
                    row = st.session_state.giro_corrente.iloc[idx]
                    
                    st.markdown(f"""
                    <div class="stop-card">
                        <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                        <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                        <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
                    </div>
                    """, unsafe_allow_html=True)

                    col_c1, col_c2, col_c3 = st.columns([1, 1, 1])
                    
                    with col_c1:
                        dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
                        st.write("")
                        st.markdown(f"[🚘 **NAVIGA ORA**](https://www.google.com/maps/dir/?api=1&destination={dest})")

                    with col_c2:
                        nuova_qta = st.number_input(
                            "Q.tà colli",
                            min_value=0,
                            value=int(row['Q.ta']),
                            key=f"qta_mobile_{row['CLIENTE']}_{idx}"
                        )
                        if nuova_qta != int(row['Q.ta']):
                            st.session_state.giro_corrente.at[idx, 'Q.ta'] = nuova_qta
                            salva_giro_su_disco(st.session_state.giro_corrente)
                            st.rerun()

                    with col_c3:
                        nuova_pos = st.selectbox(
                            "Sposta a pos:",
                            options=[i for i in range(1, tot_clienti + 1)],
                            index=idx,
                            key=f"select_pos_{row['CLIENTE']}_{idx}"
                        )
                        
                        if nuova_pos - 1 != idx:
                            df_temp = st.session_state.giro_corrente.copy()
                            riga = df_temp.iloc[idx]
                            df_temp = df_temp.drop(df_temp.index[idx])
                            top = df_temp.iloc[:nuova_pos - 1]
                            bottom = df_temp.iloc[nuova_pos - 1:]
                            
                            df_nuovo = pd.concat([top, pd.DataFrame([riga]), bottom], ignore_index=True)
                            df_nuovo['POSIZIONE'] = [str(i) for i in range(1, len(df_nuovo) + 1)]
                            
                            st.session_state.giro_corrente = df_nuovo
                            salva_giro_su_disco(st.session_state.giro_corrente)
                            st.rerun()

                    st.markdown("<hr style='margin: 10px 0; border-color: #262626;'>", unsafe_allow_html=True)

                st.markdown("---")
                st.markdown(f'''
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                ''', unsafe_allow_html=True)
        else:
            st.info("Nessuna fermata nel giro corrente. Clicca in alto su '📁 CLIENTI' per aggiungerne.")

    # ==========================================
    # SCHERMATA 2: INSERISCI CLIENTE
    # ==========================================
    elif st.session_state.pagina_attiva == "db":
        st.subheader("📁 Inserisci Clienti nel Giro")
        
        # PROTETTO: Solo l'Admin vede i pulsanti e l'area di caricamento file/reset
        if st.session_state.is_admin:
            caricamento_file = st.file_uploader("Carica Database (Excel o CSV)", type=["xlsx", "csv"])
            
            if caricamento_file is not None:
                try:
                    estensione = ".csv" if caricamento_file.name.endswith('.csv') else ".xlsx"
                    file_salvataggio = "database.csv" if estensione == ".csv" else "database.xlsx"
                    
                    with open(file_salvataggio, "wb") as f:
                        f.write(caricamento_file.getbuffer())

                    if estensione == '.csv':
                        df_up = pd.read_csv(file_salvataggio)
                    else:
                        df_up = pd.read_excel(file_salvataggio)
                    
                    st.session_state.db_clienti = elabora_dataframe_db(df_up)
                    salva_db_su_disco(st.session_state.db_clienti)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success(f"File caricato e salvato sul server! ({len(st.session_state.db_clienti)} clienti trovati)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Errore nella lettura/salvataggio del file: {e}")

            if st.button("RESET DATABASE", use_container_width=True):
                if os.path.exists(FILE_DB_PERSISTENTE):
                    os.remove(FILE_DB_PERSISTENTE)
                st.session_state.db_clienti = carica_db_predefinito()
                st.session_state.clienti_selezionati_m = []
                st.success("Database ricaricato correttamente dal server!")
                st.rerun()
            
            st.markdown("---")

        if not st.session_state.db_clienti.empty:
            lista_completa = st.session_state.db_clienti['CLIENTE'].dropna().tolist()

            def aggiorna_selezione():
                st.session_state.clienti_selezionati_m = st.session_state.widget_multiselect

            clienti_selezionati = st.multiselect(
                "Cerca e seleziona i clienti per le consegne:",
                options=lista_completa,
                default=st.session_state.clienti_selezionati_m,
                key="widget_multiselect",
                on_change=aggiorna_selezione
            )

            if clienti_selezionati:
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("### 📦 Configura Colli per i Clienti Selezionati")
                
                df_sel = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                df_sel['Q.ta'] = df_sel['QTA_DEFAULT']
                
                df_edit_colli = st.data_editor(
                    df_sel[['CLIENTE', 'COMUNE', 'Q.ta']],
                    hide_index=True,
                    use_container_width=True,
                    key="editor_colli_scelti"
                )

                if st.button("➕ CONFERMA E AGGIUNGI AL GIRO", use_container_width=True, type="primary"):
                    nuovi_clienti = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                    
                    qta_dict = dict(zip(df_edit_colli['CLIENTE'], df_edit_colli['Q.ta']))
                    nuovi_clienti['Q.ta'] = nuovi_clienti['CLIENTE'].map(qta_dict)
                    
                    nuovi_clienti = nuovi_clienti[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                    
                    st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, nuovi_clienti], ignore_index=True)
                    st.session_state.giro_corrente['POSIZIONE'] = range(1, len(st.session_state.giro_corrente) + 1)
                    
                    salva_giro_su_disco(st.session_state.giro_corrente)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success("Clienti aggiunti al giro con successo!")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()
                    
            # PROTETTO: Solo l'Admin può visualizzare/modificare l'intera anagrafica
            if st.session_state.is_admin:
                st.markdown("---")
                with st.expander("👀 Visualizza o Modifica Anagrafica Clienti intera"):
                    edited_db = st.data_editor(
                        st.session_state.db_clienti,
                        num_rows="dynamic",
                        use_container_width=True,
                        key="db_editor_switch"
                    )
                    if not edited_db.equals(st.session_state.db_clienti):
                        st.session_state.db_clienti = elabora_dataframe_db(edited_db)
                        salva_db_su_disco(st.session_state.db_clienti)
                        st.rerun()
        else:
            st.warning("Nessun cliente in memoria. Chiedi all'amministratore di caricare il file Excel per iniziare.")

    # ==========================================
    # SCHERMATA 3: GESTIONE UTENTI (SOLO ADMIN)
    # ==========================================
    elif st.session_state.pagina_attiva == "utenti" and st.session_state.is_admin:
        st.subheader("🔑 Gestione Utenti Registrati")
        st.markdown("<p style='color: #94A3B8; font-size: 14px;'>Aggiungi o rimuovi gli account autorizzati ad accedere all'app.</p>", unsafe_allow_html=True)

        with st.form("form_nuovo_utente"):
            st.markdown("### ➕ Aggiungi Nuovo Utente")
            nuovo_user = st.text_input("Nome Utente")
            nuova_pass = st.text_input("Password", type="password")
            btn_aggiungi = st.form_submit_button("Crea Utente", use_container_width=True, type="primary")

            if btn_aggiungi:
                nuovo_user = nuovo_user.strip()
                if not nuovo_user or not nuova_pass:
                    st.error("Inserisci sia il nome utente che la password.")
                elif nuovo_user in st.session_state.utenti_sistema:
                    st.warning(f"L'utente '{nuovo_user}' esiste già.")
                else:
                    st.session_state.utenti_sistema[nuovo_user] = nuova_pass
                    salva_utenti(st.session_state.utenti_sistema)
                    st.success(f"Utente '{nuovo_user}' creato con successo!")
                    st.rerun()

        st.markdown("---")
        st.markdown("### 📋 Elenco Utenti Attivi")
        
        for usr in list(st.session_state.utenti_sistema.keys()):
            col_u1, col_u2 = st.columns([3, 1])
            with col_u1:
                st.text(f"👤 {usr}")
            with col_u2:
                if usr.lower() != "admin":
                    if st.button("🗑️ Elimina", key=f"del_{usr}", use_container_width=True):
                        del st.session_state.utenti_sistema[usr]
                        salva_utenti(st.session_state.utenti_sistema)
                        st.success(f"Utente '{usr}' eliminato.")
                        st.rerun()
                else:
                    st.markdown("<span style='color: #60A5FA; font-size: 12px;'>Protetto</span>", unsafe_allow_html=True)
