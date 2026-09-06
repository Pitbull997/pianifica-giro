import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64
import secrets

try:
    import extra_streamlit_components as stx
except ImportError:
    stx = None
import time
import gspread
from google.oauth2.service_account import Credentials

# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Sessione persistente per singolo browser/dispositivo
COOKIE_NAME = "vango_session_token"
COOKIE_MAX_AGE_DAYS = 365

@st.cache_resource
def get_cookie_manager():
    if stx is None:
        return None
    return stx.CookieManager(key="vango_cookie_manager")

cookie_manager = get_cookie_manager()

def genera_token_sessione():
    return secrets.token_urlsafe(32)

def leggi_token_sessione():
    if cookie_manager is None:
        return None
    try:
        return cookie_manager.get(cookie=COOKIE_NAME)
    except Exception:
        return None

def salva_token_sessione(token):
    if cookie_manager is None or not token:
        return
    try:
        cookie_manager.set(
            COOKIE_NAME, token,
            max_age=COOKIE_MAX_AGE_DAYS * 24 * 60 * 60,
            path="/"
        )
    except Exception:
        pass

def elimina_token_sessione():
    if cookie_manager is None:
        return
    try:
        cookie_manager.delete(COOKIE_NAME)
    except Exception:
        pass

# Inizializzazione Connessione Google Sheets tramite Streamlit Secrets
@st.cache_resource
def init_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
    return client

# Connessione al foglio Google e alle relative schede
try:
    client_gs = init_google_sheets()
    sh = client_gs.open("VanGo Database")
    
    try:
        sheet_db = sh.worksheet("Foglio1")
    except Exception:
        sheet_db = sh.get_worksheet(0) # Fallback di sicurezza sulla prima scheda
        
    try:
        sheet_utenti = sh.worksheet("Utenti") # Seconda scheda: Utenti
    except Exception:
        sheet_utenti = None
    try:
        sheet_giro = sh.worksheet("GiroAttivo") # Terza scheda: Giro Attivo
    except Exception:
        sheet_giro = None
except Exception as e:
    st.error(f"⚠️ Errore di connessione a Google Sheets: {e}")
    sheet_db = None
    sheet_utenti = None
    sheet_giro = None

# Funzioni per caricare e salvare gli utenti da Google Sheets (TTL ottimizzato a 300s)
@st.cache_data(ttl=300, show_spinner=False)
def carica_utenti_da_sheets():
    utenti_default = {"admin": "vango2026", "autista": "consegne2026"}
    try:
        if sheet_utenti:
            data = sheet_utenti.get_all_records()
            if data:
                dict_utenti = {}
                for row in data:
                    row_clean = {str(k).strip().upper(): str(v).strip() for k, v in row.items()}
                    usr = row_clean.get("USERNAME", "")
                    pwd = row_clean.get("PASSWORD", "")
                    if usr:
                        dict_utenti[usr] = pwd
                if dict_utenti:
                    return dict_utenti
    except Exception as e:
        st.error(f"Errore di lettura utenti da Google Sheets: {e}")
    return utenti_default

def salva_utenti_su_sheets(dict_utenti):
    try:
        if sheet_utenti:
            time.sleep(1.0)
            sheet_utenti.clear()
            data_to_update = [["USERNAME", "PASSWORD"]] + [[u, p] for u, p in dict_utenti.items()]
            sheet_utenti.update(data_to_update)
            st.cache_data.clear()
    except Exception as e:
        st.error(f"Errore nel salvataggio utenti su Google Sheets: {e}")

# Funzioni di utilità per i dati
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
    if df.empty:
        return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])
    
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

def salva_db_su_google_sheets(df):
    try:
        if sheet_db:
            time.sleep(1.0)
            sheet_db.clear()
            data_to_update = [df.columns.values.tolist()] + df.astype(str).values.tolist()
            sheet_db.update(data_to_update)
            st.cache_data.clear()
    except Exception as e:
        st.error(f"Errore nel salvataggio su Google Sheets: {e}")

# Database Clienti con TTL ottimizzato a 300s
@st.cache_data(ttl=300, show_spinner=False)
def carica_db_da_google_sheets_cached():
    try:
        if sheet_db:
            valori_grezzi = sheet_db.get_all_values()
            if not valori_grezzi:
                intestazioni_default = ['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT']
                sheet_db.update([intestazioni_default])
                return pd.DataFrame(columns=intestazioni_default)
            
            data = sheet_db.get_all_records()
            if data:
                df = pd.DataFrame(data)
                return elabora_dataframe_db(df)
    except Exception as e:
        st.error(f"Errore di lettura da Google Sheets: {e}")
    return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

def carica_db_da_google_sheets():
    return carica_db_da_google_sheets_cached()

# --- Gestione Giro per singolo utente su Google Sheets (TTL ottimizzato a 120s) ---
@st.cache_data(ttl=120, show_spinner=False)
def carica_tutti_i_giri_da_sheets():
    try:
        if sheet_giro:
            data = sheet_giro.get_all_records()
            if data:
                return pd.DataFrame(data)
    except Exception as e:
        pass
    return pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

def carica_giro_utente_da_sheets(nome_utente):
    cols_giro = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
    df_vuoto = pd.DataFrame(columns=cols_giro)
    try:
        df = carica_tutti_i_giri_da_sheets()
        if not df.empty:
            df.columns = df.columns.str.strip().str.upper()
            if 'UTENTE' not in df.columns:
                return df_vuoto
            
            df_utente = df[df['UTENTE'].astype(str).str.strip().str.lower() == nome_utente.strip().lower()].copy()
            
            if 'Q.TA' in df_utente.columns and 'Q.TA' not in cols_giro:
                df_utente = df_utente.rename(columns={'Q.TA': 'Q.ta'})
            
            for c in cols_giro:
                if c not in df_utente.columns:
                    df_utente[c] = ""
            
            df_utente = df_utente[cols_giro]
            if not df_utente.empty and len(df_utente.dropna(how='all')) > 0:
                df_utente['POSIZIONE'] = [str(i) for i in range(1, len(df_utente) + 1)]
                return df_utente.reset_index(drop=True)
    except Exception as e:
        st.error(f"Errore di lettura del giro da Google Sheets: {e}")
    return df_vuoto

def salva_giro_utente_su_sheets(nome_utente, df_nuovo_giro):
    for tentativo in range(5):
        try:
            if sheet_giro:
                time.sleep(1.5 * (tentativo + 1))
                
                data_totale = sheet_giro.get_all_records()
                df_tutti = pd.DataFrame(data_totale) if data_totale else pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                
                if not df_tutti.empty:
                    df_tutti.columns = df_tutti.columns.str.strip().str.upper()
                    if 'Q.TA' in df_tutti.columns:
                        df_tutti = df_tutti.rename(columns={'Q.TA': 'Q.ta'})
                    df_tutti = df_tutti[df_tutti['UTENTE'].astype(str).str.strip().str.lower() != nome_utente.strip().lower()]
                
                if not df_nuovo_giro.empty:
                    df_agg = df_nuovo_giro.copy()
                    df_agg['UTENTE'] = nome_utente
                    df_agg['POSIZIONE'] = range(1, len(df_agg) + 1)
                    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
                    for c in cols_ordine:
                        if c not in df_agg.columns:
                            df_agg[c] = ""
                    df_agg = df_agg[cols_ordine]
                    
                    if df_tutti.empty:
                        df_tutti = df_agg
                    else:
                        for c in cols_ordine:
                            if c not in df_tutti.columns:
                                df_tutti[c] = ""
                        df_tutti = pd.concat([df_tutti[cols_ordine], df_agg[cols_ordine]], ignore_index=True)
                
                sheet_giro.clear()
                intestazioni = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
                if df_tutti.empty:
                    sheet_giro.update([intestazioni])
                else:
                    data_to_update = [intestazioni] + df_tutti.astype(str).values.tolist()
                    sheet_giro.update(data_to_update)
                
                st.cache_data.clear()
                return
        except Exception as e:
            if "429" in str(e) and tentativo < 4:
                continue
            elif tentativo == 4:
                st.error(f"Errore nel salvataggio del giro su Google Sheets dopo vari tentativi: {e}")
            else:
                st.error(f"Errore nel salvataggio del giro su Google Sheets: {e}")
                break

# Inizializzazione dati di sessione.
# Il token nel cookie appartiene al singolo browser/dispositivo.
if "sessioni_persistenti" not in st.session_state:
    st.session_state.sessioni_persistenti = {}

if 'autenticato' not in st.session_state:
    st.session_state.autenticato = False

if 'utente_corrente' not in st.session_state:
    st.session_state.utente_corrente = ""

if 'is_admin' not in st.session_state:
    st.session_state.is_admin = False

if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "welcome"

if 'session_token' not in st.session_state:
    st.session_state.session_token = leggi_token_sessione()

# Ripristina il login quando il browser/app viene riaperto.
if (
    not st.session_state.autenticato
    and st.session_state.session_token
    and st.session_state.session_token in st.session_state.sessioni_persistenti
):
    dati = st.session_state.sessioni_persistenti[st.session_state.session_token]
    st.session_state.autenticato = True
    st.session_state.utente_corrente = dati["utente"]
    st.session_state.is_admin = dati["is_admin"]
    st.session_state.pagina_attiva = "giro"

if 'db_clienti' not in st.session_state:
    st.session_state.db_clienti = carica_db_da_google_sheets()

if 'utenti_sistema' not in st.session_state:
    st.session_state.utenti_sistema = carica_utenti_da_sheets()

if 'giro_corrente' not in st.session_state or st.session_state.get('ultimo_utente_caricato') != st.session_state.utente_corrente:
    if st.session_state.utente_corrente:
        st.session_state.giro_corrente = carica_giro_utente_da_sheets(st.session_state.utente_corrente)
        st.session_state.ultimo_utente_caricato = st.session_state.utente_corrente
    else:
        st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

if 'clienti_selezionati_m' not in st.session_state:
    st.session_state.clienti_selezionati_m = []

if 'vista_pulita' not in st.session_state:
    st.session_state.vista_pulita = False

if "nav" in st.query_params and st.query_params["nav"] == "login":
    st.session_state.pagina_attiva = "login"
    st.query_params.clear()

# CSS Avanzato
st.markdown("""
<style>
    .stApp, body, html {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }
    header {visibility: hidden;}
    .stMainBlockContainer { padding: 0rem !important; max-width: 100% !important; }
    .block-container { padding-top: 0.5rem !important; padding-bottom: 1rem !important; max-width: 100% !important; }

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

    div[data-testid="stHorizontalBlock"] { gap: 0.5rem !important; margin-bottom: -0.5rem !important; }
    div[data-testid="column"] { margin-bottom: 0px !important; }

    [data-testid="stMetricLabel"] { color: #CCCCCC !important; font-size: 14px !important; font-weight: 600 !important; }
    [data-testid="stMetricValue"] { color: #FFFFFF !important; font-size: 28px !important; font-weight: bold !important; }

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

    div[data-baseweb="select"] { background-color: #1E293B !important; border-radius: 8px !important; }
    div[data-baseweb="select"] > div { background-color: #1E293B !important; color: #FFFFFF !important; border: 1px solid #3B82F6 !important; border-radius: 8px !important; }

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

# ==========================================
# SCHERMATA 0: WELCOME / HOME PAGE
# ==========================================
if not st.session_state.autenticato and st.session_state.pagina_attiva == "welcome":
    img_path = "vango_splash.png"
    if os.path.exists(img_path):
        with open(img_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode()
        
        st.markdown(f"""
        <style>
            .hero-fullscreen {{
                position: fixed;
                top: 0; left: 0;
                width: 100vw; height: 100vh;
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
                bottom: 6%; left: 50%;
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
                width: 85%; max-width: 400px;
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
elif not st.session_state.autenticato and st.session_state.pagina_attiva == "login":
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
                st.session_state.utenti_sistema = carica_utenti_da_sheets()
                utenti_validi = st.session_state.utenti_sistema
                username_input = username_input.strip()

                if username_input in utenti_validi and utenti_validi[username_input] == password_input:
                    st.session_state.autenticato = True
                    st.session_state.utente_corrente = username_input
                    st.session_state.is_admin = (username_input.lower() == "admin")
                    st.session_state.pagina_attiva = "giro"


                    # Token univoco per questo browser/dispositivo.
                    token = genera_token_sessione()
                    st.session_state.session_token = token
                    st.session_state.sessioni_persistenti[token] = {
                        "utente": username_input,
                        "is_admin": st.session_state.is_admin
                    }
                    salva_token_sessione(token)
                    
                    st.session_state.giro_corrente = carica_giro_utente_da_sheets(username_input)
                    st.session_state.ultimo_utente_caricato = username_input
                    
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

    col_info_u, col_logout_u = st.columns([3, 1])
    with col_info_u:
        st.markdown(f"<p style='color: #94A3B8; font-size: 13px; margin: 0;'>👤 Utente: <b style='color: #60A5FA;'>{st.session_state.get('utente_corrente', '')}</b></p>", unsafe_allow_html=True)
    with col_logout_u:
        if st.button("🚪 LOGOUT", use_container_width=True, key="btn_logout_principale"):
            token_corrente = st.session_state.get("session_token")
            if token_corrente:
                st.session_state.sessioni_persistenti.pop(token_corrente, None)
            elimina_token_sessione()
            st.session_state.session_token = None
            st.session_state.autenticato = False
            st.session_state.utente_corrente = ""
            st.session_state.is_admin = False
            st.session_state.pagina_attiva = "login"
            st.rerun()

    st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

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
                st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
                salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_act2:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("🗑️ SVUOTA GIRO", use_container_width=True, key="btn_svuota"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
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
                
                if len(addresses) > 2:
                    waypoints = "/".join([urllib.parse.quote(a) for a in addresses[1:-1]])
                    maps_url = f"https://www.google.com/maps/dir/{origin}/{waypoints}/{destination}"
                else:
                    maps_url = f"https://www.google.com/maps/dir/{origin}/{destination}"

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
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
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
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
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
            st.info("Nessuna fermata nel tuo giro corrente. Clicca in alto su '📁 CLIENTI' per aggiungerne.")

    # ==========================================
    # SCHERMATA 2: INSERISCI CLIENTE
    # ==========================================
    elif st.session_state.pagina_attiva == "db":
        st.subheader("📁 Inserisci Clienti nel Tuo Giro")
        
        # Pulsante universale per forzare l'aggiornamento e svuotare la cache
        if st.button("🔄 Forza Aggiornamento / Svuota Cache", use_container_width=True):
            st.cache_data.clear()
            st.session_state.db_clienti = carica_db_da_google_sheets()
            st.success("Cache svuotata e dati ricaricati con successo!")
            st.rerun()
            
        st.markdown("<br>", unsafe_allow_html=True)
        
        if st.session_state.is_admin:
            caricamento_file = st.file_uploader("Carica Database Clienti su Google Sheets (Excel o CSV)", type=["xlsx", "csv"])
            
            if caricamento_file is not None:
                try:
                    if caricamento_file.name.endswith('.csv'):
                        df_up = pd.read_csv(caricamento_file)
                    else:
                        df_up = pd.read_excel(caricamento_file)
                    
                    st.session_state.db_clienti = elabora_dataframe_db(df_up)
                    salva_db_su_google_sheets(st.session_state.db_clienti)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success(f"Database caricato e sincronizzato su Google Sheets! ({len(st.session_state.db_clienti)} clienti)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Errore nel caricamento del file: {e}")
            
            st.markdown("---")

        if not st.session_state.db_clienti.empty:
            lista_completa = st.session_state.db_clienti['CLIENTE'].dropna().tolist()

            def aggiorna_selezione():
                st.session_state.clienti_selezionati_m = st.session_state.widget_multiselect

            clienti_selezionati = st.multiselect(
                "Cerca e seleziona i clienti per le tue consegne:",
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

                if st.button("➕ CONFERMA E AGGIUNGI AL MIO GIRO", use_container_width=True, type="primary"):
                    nuovi_clienti = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                    
                    qta_dict = dict(zip(df_edit_colli['CLIENTE'], df_edit_colli['Q.ta']))
                    nuovi_clienti['Q.ta'] = nuovi_clienti['CLIENTE'].map(qta_dict)
                    
                    nuovi_clienti = nuovi_clienti[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']] if 'POSIZIONE' in nuovi_clienti.columns else nuovi_clienti[['CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                    
                    st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, nuovi_clienti], ignore_index=True)
                    st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
                    
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success("Clienti aggiunti al tuo giro e salvati su Google Sheets!")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()
                
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
                        salva_db_su_google_sheets(st.session_state.db_clienti)
                        st.rerun()
        else:
            st.warning("Nessun cliente trovato su Google Sheets.")

    # ==========================================
    # SCHERMATA 3: GESTIONE UTENTI (SOLO ADMIN)
    # ==========================================
    elif st.session_state.pagina_attiva == "utenti" and st.session_state.is_admin:
        st.subheader("🔑 Gestione Utenti da Google Sheets")
        st.markdown("<p style='color: #94A3B8; font-size: 14px;'>Gestisci gli account autorizzati direttamente dal foglio Google dedicato.</p>", unsafe_allow_html=True)

        dict_u = carica_utenti_da_sheets()
        df_utenti_attuali = pd.DataFrame(list(dict_u.items()), columns=["USERNAME", "PASSWORD"])

        edited_utenti = st.data_editor(
            df_utenti_attuali,
            num_rows="dynamic",
            use_container_width=True,
            key="editor_utenti_sheets"
        )

        if st.button("💾 SALVA MODIFICHE UTENTI SU GOOGLE SHEETS", use_container_width=True, type="primary"):
            nuovo_dict = {}
            for _, row in edited_utenti.iterrows():
                u = str(row["USERNAME"]).strip()
                p = str(row["PASSWORD"]).strip()
                if u and u.lower() != "nan":
                    nuovo_dict[u] = p
            
            if "admin" not in nuovo_dict:
                nuovo_dict["admin"] = "vango2026"

            salva_utenti_su_sheets(nuovo_dict)
            st.session_state.utenti_sistema = nuovo_dict
            st.success("Tabella utenti aggiornata e salvata su Google Sheets con successo!")
            st.rerun()
