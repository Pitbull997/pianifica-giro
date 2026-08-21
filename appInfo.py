import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64

# Configurazione Pagina
st.set_page_config(
    page_title="Giro Consegne",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

FILE_GIRO_PERSISTENTE = "giro_salvato.json"

# Funzione per convertire l'immagine in base64
def get_base64_of_bin_file(bin_file):
    with open(bin_file, 'rb') as f:
        data = f.read()
    return base64.b64encode(data).decode()

# Caricamento sfondo
bg_css = ""
if os.path.exists("sfondo.jpg"):
    bin_str = get_base64_of_bin_file("sfondo.jpg")
    bg_css = f"""
    .stApp {{
        background-image: linear-gradient(rgba(11, 15, 25, 0.88), rgba(11, 15, 25, 0.94)), url("data:image/jpeg;base64,{bin_str}");
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
        background-attachment: fixed;
    }}
    """

# CSS personalizzato
st.markdown(f"""
<style>
    {bg_css}
    .stApp {{ background-color: #0B0F19 !important; color: #FFFFFF !important; }}
    [data-testid="stMetricLabel"] {{ color: #94A3B8 !important; }}
    [data-testid="stMetricValue"] {{ color: #38BDF8 !important; }}
    .stop-card {{
        background: rgba(22, 30, 46, 0.9);
        backdrop-filter: blur(8px);
        border-left: 5px solid #2563EB;
        padding: 12px 14px;
        border-radius: 10px;
        margin-bottom: 10px;
        border: 1px solid #334155;
    }}
    .stop-title {{ font-size: 17px; font-weight: bold; color: #FFFFFF; }}
</style>
""", unsafe_allow_html=True)

# Funzioni di utilità
def pulisci_orario(valore):
    val_str = str(valore).strip()
    return val_str[:5] if len(val_str) >= 5 else val_str

def carica_db():
    if os.path.exists("database.csv"): return pd.read_csv("database.csv")
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

def salva_giro(df): df.to_json(FILE_GIRO_PERSISTENTE, orient="records")

def carica_giro():
    if os.path.exists(FILE_GIRO_PERSISTENTE):
        return pd.read_json(FILE_GIRO_PERSISTENTE, orient="records")
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Session State
if 'db_clienti' not in st.session_state: st.session_state.db_clienti = carica_db()
if 'giro_corrente' not in st.session_state: st.session_state.giro_corrente = carica_giro()
if 'pagina_attiva' not in st.session_state: st.session_state.pagina_attiva = "maps"

# Navigazione
col_n1, col_n2 = st.columns(2)
if col_n1.button("🗺️ Maps", use_container_width=True): st.session_state.pagina_attiva = "maps"
if col_n2.button("➕ Inserisci", use_container_width=True): st.session_state.pagina_attiva = "inserisci"
st.markdown("---")

# Logica Mappa
if st.session_state.pagina_attiva == "maps":
    st.subheader("🗺️ Giro Consegne Attivo")
    
    if not st.session_state.giro_corrente.empty:
        tot = len(st.session_state.giro_corrente)
        
        for idx in range(tot):
            row = st.session_state.giro_corrente.iloc[idx]
            
            st.markdown(f"""<div class="stop-card">
                <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                <div style='color:#E2E8F0;'>📍 {row['VIA']}, {row['COMUNE']}</div>
            </div>""", unsafe_allow_html=True)
            
            # Menu a tendina che riordina al volo
            nuova_pos = st.selectbox(
                "Sposta ordine",
                options=range(1, tot + 1),
                index=idx,
                key=f"pos_{idx}",
                label_visibility="collapsed"
            )
            
            if nuova_pos - 1 != idx:
                righe = st.session_state.giro_corrente.to_dict('records')
                elemento = righe.pop(idx)
                righe.insert(nuova_pos - 1, elemento)
                
                df_agg = pd.DataFrame(righe)
                df_agg['POSIZIONE'] = range(1, len(df_agg) + 1)
                st.session_state.giro_corrente = df_agg
                salva_giro(st.session_state.giro_corrente)
                st.rerun()
            st.write("")

# Logica Inserisci (Semplificata)
elif st.session_state.pagina_attiva == "inserisci":
    st.subheader("🔍 Aggiungi Fermata")
    # ... (inserisci qui la logica di aggiunta clienti)
