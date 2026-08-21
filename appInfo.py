import streamlit as st
import pandas as pd
import urllib.parse
import os

# Configurazione Pagina
st.set_page_config(
    page_title="Giro Consegne",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Styling CSS Avanzato per Stile Nativo/Dark
st.markdown("""
<style>
    /* Sfondo generale e font */
    .stApp {
        background-color: #121212;
        color: #FFFFFF;
    }
    
    /* Card fermata stile Timeline */
    .stop-card {
        background-color: #1E1E1E;
        border-left: 4px solid #2563EB;
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 10px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    .stop-title {
        font-size: 18px;
        font-weight: bold;
        color: #FFFFFF;
        margin-bottom: 2px;
    }
    .stop-address {
        font-size: 14px;
        color: #A0A0A0;
        margin-bottom: 6px;
    }
    .stop-meta {
        font-size: 13px;
        color: #60A5FA;
        font-weight: 600;
    }

    /* Pulsante d'azione principale azzurro/blu */
    div.stButton > button {
        background-color: #2563EB !important;
        color: white !important;
        border-radius: 25px !important;
        height: 48px !important;
        font-size: 16px !important;
        font-weight: bold !important;
        border: none !important;
        box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4) !important;
    }
    
    /* Reset e piccoli bottoni secondari */
    div[data-testid="stExpander"] {
        background-color: #1E1E1E;
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)

def pulisci_orario(valore):
    val_str = str(valore).strip()
    if 'days' in val_str:
        val_str = val_str.split()[-1]
    if len(val_str) >= 5:
        return val_str[:5]
    return val_str

def carica_db_predefinito():
    nomi_file_possibili = ["database.xlsx", "database.csv", "database"]
    for file_path in nomi_file_possibili:
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path) if file_path.endswith('.csv') else pd.read_excel(file_path)
                df.columns = df.columns.str.strip().str.upper()
                df['POSIZIONE'] = pd.to_numeric(df['POSIZIONE'], errors='coerce').fillna(0).astype(int)
                df['QTA_DEFAULT'] = pd.to_numeric(df['QTA_DEFAULT'], errors='coerce').fillna(0).astype(int)
                df['CLIENTE'] = df['CLIENTE'].astype(str)
                df['COMUNE'] = df['COMUNE'].astype(str)
                df['VIA'] = df['VIA'].astype(str)
                df['ORA'] = df['ORA'].apply(pulisci_orario)
                return df.sort_values(by="POSIZIONE").reset_index(drop=True)
            except Exception as e:
                st.error(f"Errore caricamento {file_path}: {e}")
    return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

if 'db_clienti' not in st.session_state or st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_db_predefinito()

if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Menu in alto compatto
st.sidebar.title("🚚 Menu")
page = st.sidebar.radio("Schermata", ["Pianificazione Giro", "Database Clienti"])

if page == "Pianificazione Giro":
    st.title("📍 Giro del Giorno")

    tot_clienti = len(st.session_state.giro_corrente)
    tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum()) if not st.session_state.giro_corrente.empty else 0

    col_m1, col_m2 = st.columns(2)
    col_m1.metric("Fermate", f"{tot_clienti}")
    col_m2.metric("Pezzi Totali", f"{tot_qta}")

    st.markdown("---")

    if not st.session_state.giro_corrente.empty:
        # Visualizzazione fermate in stile timeline verticale
        for idx, row in st.session_state.giro_corrente.iterrows():
            st.markdown(f"""
            <div class="stop-card">
                <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
            </div>
            """, unsafe_allow_html=True)
            
            # Bottone di navigazione diretta per singola tappa
            dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
            st.markdown(f"[🚘 **Naviga verso Tappa {idx + 1}**](https://www.google.com/maps/dir/?api=1&destination={dest})")
            st.write("")

        st.markdown("---")

        # Bottone principale in basso stile screenshot per apri mappa completa
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
                <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:50px; font-weight:bold; font-size:16px;">
                    🗺️ AVVIA PERCORSO COMPLETO
                </button>
            </a>
        ''', unsafe_allow_html=True)

        with st.expander("⚙️ Gestisci / Riordina Fermate"):
            if st.button("🔄 Inverti Sequenza Giro"):
                st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                st.rerun()
            if st.button("🗑️ Svuota Giro"):
                st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                st.rerun()
    else:
        st.info("Nessuna fermata nel giro. Seleziona i clienti dal Database.")

elif page == "Database Clienti":
    st.title("📁 Anagrafica Clienti")
    
    if not st.session_state.db_clienti.empty:
        lista_completa = st.session_state.db_clienti['CLIENTE'].dropna().tolist()
        
        clienti_selezionati = st.multiselect(
            "Cerca e seleziona i clienti per il giro:",
            options=lista_completa
        )
        
        if st.button("➕ AGGIUNGI AL GIRO"):
            if clienti_selezionati:
                agg = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                agg['Q.ta'] = agg['QTA_DEFAULT'].astype(int)
                agg = agg[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, agg], ignore_index=True)
                st.success("Aggiunti al giro con successo!")
                st.rerun()
    else:
        st.warning("Carica il file database.xlsx su GitHub.")
