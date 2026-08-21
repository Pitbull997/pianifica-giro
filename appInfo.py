import streamlit as st
import pandas as pd
import urllib.parse
import os
import json
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

# CSS personalizzato per lo stile e la barra inferiore
st.markdown(f"""
<style>
    {bg_css}

    .stApp, body, html {{
        background-color: #0B0F19 !important;
        color: #FFFFFF !important;
    }}

    [data-testid="stMetricLabel"] {{ color: #94A3B8 !important; font-size: 13px !important; font-weight: 600 !important; }}
    [data-testid="stMetricValue"] {{ color: #38BDF8 !important; font-size: 28px !important; font-weight: bold !important; }}

    div[data-testid="stButton"] > button {{
        background-color: rgba(30, 41, 59, 0.9) !important;
        color: #FFFFFF !important;
        border: 1px solid #475569 !important;
        border-radius: 8px !important;
        font-weight: bold !important;
        -webkit-appearance: none !important;
    }}

    .stop-card {{
        background: rgba(22, 30, 46, 0.9);
        backdrop-filter: blur(8px);
        border-left: 5px solid #2563EB;
        padding: 12px 14px;
        border-radius: 10px;
        margin-top: 10px;
        border-top: 1px solid #334155;
        border-right: 1px solid #334155;
        border-bottom: 1px solid #334155;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
    }}
    .stop-title {{ font-size: 17px; font-weight: bold; color: #FFFFFF; margin-bottom: 4px; }}
    .stop-address {{ font-size: 14px; color: #E2E8F0; margin-bottom: 6px; }}
    .stop-meta {{ font-size: 13px; color: #60A5FA; font-weight: 600; }}

    div[data-testid="stExpander"] {{
        background-color: rgba(30, 41, 59, 0.9) !important;
        border-radius: 10px !important;
        border: 1px solid #334155 !important;
    }}
</style>
""", unsafe_allow_html=True)

def pulisci_orario(valore):
    val_str = str(valore).strip()
    if 'days' in val_str:
        val_str = val_str.split()[-1]
    if len(val_str) >= 5:
        return val_str[:5]
    return val_str

# Caricamento database
def carica_db_predefinito():
    nomi_file_possibili = ["database.xlsx", "database.csv", "database"]
    for file_path in nomi_file_possibili:
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path) if file_path.endswith('.csv') else pd.read_excel(file_path)
                df.columns = df.columns.str.strip().str.upper()
                df['POSIZIONE'] = pd.to_numeric(df['POSIZIONE'], errors='coerce').fillna(9999).astype(int)
                df['QTA_DEFAULT'] = pd.to_numeric(df['QTA_DEFAULT'], errors='coerce').fillna(0).astype(int)
                df['CLIENTE'] = df['CLIENTE'].astype(str)
                df['COMUNE'] = df['COMUNE'].astype(str)
                df['VIA'] = df['VIA'].astype(str)
                df['ORA'] = df['ORA'].apply(pulisci_orario)
                return df.sort_values(by="POSIZIONE").reset_index(drop=True)
            except Exception as e:
                st.error(f"Errore caricamento {file_path}: {e}")
    return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

def salva_giro_su_disco(df):
    try:
        df.to_json(FILE_GIRO_PERSISTENTE, orient="records", date_format="iso")
    except Exception as e:
        st.error(f"Errore nel salvataggio: {e}")

def carica_giro_da_disco():
    if os.path.exists(FILE_GIRO_PERSISTENTE):
        try:
            df = pd.read_json(FILE_GIRO_PERSISTENTE, orient="records")
            if not df.empty:
                return df
        except Exception:
            pass
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Inizializzazione session state
if 'db_clienti' not in st.session_state or st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_db_predefinito()

if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = carica_giro_da_disco()

if 'bozza_inserimenti' not in st.session_state:
    st.session_state.bozza_inserimenti = []

if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "inserisci"

# ==========================================
# BARRA DI NAVIGAZIONE IN BASSO
# ==========================================
st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

col_nav1, col_nav2, col_nav3, col_nav4 = st.columns(4)

with col_nav1:
    if st.button("➕ Inserisci", use_container_width=True):
        st.session_state.pagina_attiva = "inserisci"
        st.rerun()
with col_nav2:
    if st.button("🗺️ Maps", use_container_width=True):
        st.session_state.pagina_attiva = "maps"
        st.rerun()
with col_nav3:
    if st.button("📋 Crea Giro", use_container_width=True):
        st.session_state.pagina_attiva = "crea_giro"
        st.rerun()
with col_nav4:
    if st.button("📁 Database", use_container_width=True):
        st.session_state.pagina_attiva = "database"
        st.rerun()

st.markdown("---")

# ==========================================
# 1. SCHERMATA: INSERISCI CLIENTE NELLA BOZZA
# ==========================================
if st.session_state.pagina_attiva == "inserisci":
    st.subheader("🔍 Aggiungi Fermata al Giro")
    
    if not st.session_state.db_clienti.empty:
        lista_clienti = st.session_state.db_clienti['CLIENTE'].tolist()
        cliente_scelto = st.selectbox("Digita o cerca il cliente:", options=lista_clienti)
        
        if cliente_scelto:
            dati_cli = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'] == cliente_scelto].iloc[0]
            
            st.markdown(f"""
            <div class="stop-card">
                <div class="stop-address">📍 Indirizzo: {dati_cli['VIA']}, {dati_cli['COMUNE']}</div>
                <div class="stop-meta">🕒 Orario solito: {dati_cli['ORA']} | 🔢 Posizione DB: {dati_cli['POSIZIONE']}</div>
            </div>
            """, unsafe_allow_html=True)
            
            qta_inserita = st.number_input("Quanti colli / pezzi?", min_value=1, value=int(dati_cli['QTA_DEFAULT']) if dati_cli['QTA_DEFAULT'] > 0 else 1)
            
            if st.button("➕ Conferma e Aggiungi alla Bozza", use_container_width=True):
                nuova_fermata = {
                    'POSIZIONE_DB': int(dati_cli['POSIZIONE']),
                    'CLIENTE': dati_cli['CLIENTE'],
                    'COMUNE': dati_cli['COMUNE'],
                    'VIA': dati_cli['VIA'],
                    'ORA': dati_cli['ORA'],
                    'Q.ta': int(qta_inserita)
                }
                st.session_state.bozza_inserimenti.append(nuova_fermata)
                st.success(f"Aggiunto: {cliente_scelto} ({qta_inserita} colli)")
                st.rerun()
                
        st.markdown("---")
        st.markdown(f"### 📦 Fermate in attesa di creazione: **{len(st.session_state.bozza_inserimenti)}**")
        
        if st.session_state.bozza_inserimenti:
            for i, f in enumerate(st.session_state.bozza_inserimenti):
                col_i1, col_i2 = st.columns([4, 1])
                with col_i1:
                    st.markdown(f"**{i+1}. {f['CLIENTE']}** ({f['Q.ta']} colli)  \n<span style='font-size:12px; color:#94A3B8;'>{f['VIA']}, {f['COMUNE']} (Pos. DB: {f['POSIZIONE_DB']})</span>", unsafe_allow_html=True)
                with col_i2:
                    if st.button("❌ Rimuovi", key=f"del_bozza_{i}", use_container_width=True):
                        st.session_state.bozza_inserimenti.pop(i)
                        st.rerun()
                st.markdown("<hr style='margin: 8px 0; border-color: #334155;'>", unsafe_allow_html=True)
            
            if st.button("🚀 CREA GIRO FINALE", use_container_width=True):
                df_nuovo_giro = pd.DataFrame(st.session_state.bozza_inserimenti)
                df_nuovo_giro = df_nuovo_giro.sort_values(by="POSIZIONE_DB").reset_index(drop=True)
                df_nuovo_giro['POSIZIONE'] = range(1, len(df_nuovo_giro) + 1)
                df_nuovo_giro = df_nuovo_giro[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                
                st.session_state.giro_corrente = df_nuovo_giro
                salva_giro_su_disco(st.session_state.giro_corrente)
                st.session_state.bozza_inserimenti = []
                st.success("Giro creato e ordinato in base al database!")
                st.session_state.pagina_attiva = "maps"
                st.rerun()
    else:
        st.warning("Database clienti vuoto.")

# ==========================================
# 2. SCHERMATA: MAPS / GIRO ATTIVO + MODIFICA RAPIDA
# ==========================================
elif st.session_state.pagina_attiva == "maps":
    st.subheader("🗺️ Giro Consegne Attivo")
    
    if not st.session_state.giro_corrente.empty:
        tot_clienti = len(st.session_state.giro_corrente)
        tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum())

        col_m1, col_m2 = st.columns(2)
        col_m1.metric("Fermate Totali", f"{tot_clienti}")
        col_m2.metric("Pezzi Totali", f"{tot_qta}")

        st.markdown("---")

        # RIQUADRO PER SPOSTARE / INVERTIRE LE FERMATE DIRETTAMENTE QUI
        with st.expander("⚙️ Modifica ordine fermate (Sposta o Inverti)"):
            num_fermate = len(st.session_state.giro_corrente)
            lista_nomi_fermate = [f"{i+1}. {row['CLIENTE']}" for i, row in st.session_state.giro_corrente.iterrows()]
            
            col_s1, col_s2, col_s3 = st.columns([2, 2, 1])
            with col_s1:
                ferm_da_spostare = st.selectbox("Sposta fermata:", options=range(num_fermate), format_func=lambda x: lista_nomi_fermate[x])
            with col_s2:
                nuova_pos = st.selectbox("Alla posizione:", options=range(1, num_fermate + 1), index=0)
            with col_s3:
                st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
                if st.button("Sposta", use_container_width=True):
                    idx_attuale = ferm_da_spostare
                    idx_nuovo = nuova_pos - 1
                    
                    righe = st.session_state.giro_corrente.to_dict('records')
                    elemento_spostato = righe.pop(idx_attuale)
                    righe.insert(idx_nuovo, elemento_spostato)
                    
                    df_aggiornato = pd.DataFrame(righe)
                    df_aggiornato['POSIZIONE'] = range(1, len(df_aggiornato) + 1)
                    st.session_state.giro_corrente = df_aggiornato
                    salva_giro_su_disco(st.session_state.giro_corrente)
                    st.success("Posizione aggiornata!")
                    st.rerun()

            st.markdown("---")
            if st.button("🔄 Inverti l'intero giro (Inverti Sequenza)", use_container_width=True):
                st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                st.session_state.giro_corrente['POSIZIONE'] = range(1, len(st.session_state.giro_corrente) + 1)
                salva_giro_su_disco(st.session_state.giro_corrente)
                st.rerun()

        st.markdown("---")

        for idx in range(tot_clienti):
            row = st.session_state.giro_corrente.iloc[idx]
            st.markdown(f"""
            <div class="stop-card">
                <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
            </div>
            """, unsafe_allow_html=True)
            
            dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
            st.markdown(f"[🚘 Naviga singola fermata](https://www.google.com/maps/dir/?api=1&destination={dest})")

        st.markdown("---")
        addresses = [f"{r['VIA']}, {r['COMUNE']}" for _, r in st.session_state.giro_corrente.iterrows()]
        if len(addresses) == 1:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(addresses[0])}"
        else:
            origin = urllib.parse.quote(addresses[0])
            destination = urllib.parse.quote(addresses[-1])
            waypoints = "|".join([urllib.parse.quote(a) for a in addresses[1:-1]])
            maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"

        st.markdown(f'''
            <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px;">
                    🗺️ AVVIA PERCORSO COMPLETO GOOGLE MAPS
                </button>
            </a>
        ''', unsafe_allow_html=True)
    else:
        st.info("Nessun giro attivo. Clicca su 'Inserisci' in basso per aggiungere i clienti.")

# ==========================================
# 3. SCHERMATA: CREA GIRO (Svuota o gestione extra)
# ==========================================
elif st.session_state.pagina_attiva == "crea_giro":
    st.subheader("📋 Gestione Giro")
    if not st.session_state.giro_corrente.empty:
        st.write("Puoi azzerare il giro corrente se devi iniziarne uno nuovo:")
        if st.button("🗑️ Svuota Giro", use_container_width=True):
            st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
            salva_giro_su_disco(st.session_state.giro_corrente)
            st.rerun()
    else:
        st.info("Il giro è attualmente vuoto.")

# ==========================================
# 4. SCHERMATA: DATABASE CLIENTI
# ==========================================
elif st.session_state.pagina_attiva == "database":
    st.subheader("📁 Anagrafica Clienti")
    if not st.session_state.db_clienti.empty:
        edited_db = st.data_editor(
            st.session_state.db_clienti,
            num_rows="dynamic",
            use_container_width=True,
            key="db_editor_main"
        )
        st.session_state.db_clienti = edited_db
    else:
        st.warning("Nessun database caricato.")
