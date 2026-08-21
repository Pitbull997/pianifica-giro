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

# CSS Avanzato per Fix Colori Pulsanti e Visibilità Testo
st.markdown("""
<style>
    /* Sfondo generale scuro */
    .stApp {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }

    /* Stile forzato per tutti i bottoni di Streamlit */
    div.stButton > button {
        background-color: #2563EB !important;
        color: #FFFFFF !important;
        border: 1px solid #3B82F6 !important;
        border-radius: 12px !important;
        height: 50px !important;
        font-weight: bold !important;
        font-size: 15px !important;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3) !important;
    }

    /* Hover e focus sui bottoni */
    div.stButton > button:hover, div.stButton > button:focus, div.stButton > button:active {
        background-color: #1D4ED8 !important;
        color: #FFFFFF !important;
        border-color: #60A5FA !important;
    }

    /* Stile per pulsante non attivo nello switcher */
    .btn-inactive button {
        background-color: #1E293B !important;
        color: #94A3B8 !important;
        border: 1px solid #334155 !important;
    }

    /* Card fermata stile Timeline */
    .stop-card {
        background-color: #1E1E1E;
        border-left: 4px solid #2563EB;
        padding: 12px 16px;
        border-radius: 10px;
        margin-bottom: 8px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    .stop-title {
        font-size: 17px;
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

    /* Modifica stile expander */
    div[data-testid="stExpander"] {
        background-color: #1E1E1E !important;
        border-radius: 10px !important;
        border: 1px solid #2A2A2A !important;
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

# Inizializzazione sessioni
if 'db_clienti' not in st.session_state or st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_db_predefinito()

if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "giro"

# ==========================================
# SWITCHER PULSANTI IN ALTO (MOBILE READY)
# ==========================================
col_sw1, col_sw2 = st.columns(2)

with col_sw1:
    if st.session_state.pagina_attiva == "giro":
        if st.button("📍 GIRO DEL GIORNO", use_container_width=True, key="btn_giro"):
            pass
    else:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("📍 GIRO DEL GIORNO", use_container_width=True, key="btn_giro"):
            st.session_state.pagina_attiva = "giro"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

with col_sw2:
    if st.session_state.pagina_attiva == "db":
        if st.button("📁 DATABASE CLIENTI", use_container_width=True, key="btn_db"):
            pass
    else:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("📁 DATABASE CLIENTI", use_container_width=True, key="btn_db"):
            st.session_state.pagina_attiva = "db"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

st.markdown("<div style='margin-bottom: 15px;'></div>", unsafe_allow_html=True)

# ==========================================
# SCHERMATA 1: GIRO CONSEGNE
# ==========================================
if st.session_state.pagina_attiva == "giro":
    
    tot_clienti = len(st.session_state.giro_corrente)
    tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum()) if not st.session_state.giro_corrente.empty else 0

    col_m1, col_m2 = st.columns(2)
    col_m1.metric("Fermate Totali", f"{tot_clienti}")
    col_m2.metric("Pezzi Totali", f"{tot_qta}")

    st.markdown("---")

    if not st.session_state.giro_corrente.empty:
        vista = st.radio("Modalità vista:", ["📱 Lista Schede (Mobile)", "✏️ Tabella Modificabile"], horizontal=True)

        if vista == "📱 Lista Schede (Mobile)":
            for idx, row in st.session_state.giro_corrente.iterrows():
                st.markdown(f"""
                <div class="stop-card">
                    <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                    <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                    <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
                </div>
                """, unsafe_allow_html=True)
                
                dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
                st.markdown(f"[🚘 **Naviga verso Tappa {idx + 1}**](https://www.google.com/maps/dir/?api=1&destination={dest})")
                st.write("")

        else:
            edited_df = st.data_editor(
                st.session_state.giro_corrente,
                column_config={
                    "POSIZIONE": st.column_config.NumberColumn("Pos.", disabled=True, format="%d"),
                    "Q.ta": st.column_config.NumberColumn("Q.tà", min_value=0, step=1, format="%d"),
                    "CLIENTE": st.column_config.TextColumn("Cliente", disabled=True),
                    "COMUNE": st.column_config.TextColumn("Comune", disabled=True),
                    "VIA": st.column_config.TextColumn("Via", disabled=True),
                    "ORA": st.column_config.TextColumn("Ora"),
                },
                num_rows="dynamic",
                use_container_width=True,
                key="giro_editor_switch"
            )
            st.session_state.giro_corrente = edited_df

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
                <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:50px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                    🗺️ AVVIA PERCORSO COMPLETO
                </button>
            </a>
        ''', unsafe_allow_html=True)

        with st.expander("⚙️ Azioni e Gestione Giro"):
            col_a1, col_a2 = st.columns(2)
            with col_a1:
                if st.button("🔄 Inverti Sequenza", use_container_width=True):
                    st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                    st.rerun()
            with col_a2:
                if st.button("🗑️ Svuota Giro", use_container_width=True):
                    st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                    st.rerun()
    else:
        st.info("Nessuna fermata nel giro corrente. Clicca in alto su 'DATABASE CLIENTI' per aggiungerne.")

# ==========================================
# SCHERMATA 2: DATABASE CLIENTI
# ==========================================
elif st.session_state.pagina_attiva == "db":
    st.subheader("📁 Database & Selezione Clienti")
    
    if not st.session_state.db_clienti.empty:
        lista_completa = st.session_state.db_clienti['CLIENTE'].dropna().tolist()
        
        col_b1, col_b2 = st.columns(2)
        with col_b1:
            if st.button("✅ Seleziona Tutti", use_container_width=True):
                st.session_state.clienti_selezionati_m = lista_completa
                st.rerun()
        with col_b2:
            if st.button("❌ Deseleziona Tutti", use_container_width=True):
                st.session_state.clienti_selezionati_m = []
                st.rerun()

        if 'clienti_selezionati_m' not in st.session_state:
            st.session_state.clienti_selezionati_m = []

        clienti_selezionati = st.multiselect(
            "Cerca e seleziona i clienti per il giro:",
            options=lista_completa,
            default=st.session_state.clienti_selezionati_m
        )
        
        if st.button("➕ AGGIUNGI SELEZIONATI AL GIRO", use_container_width=True):
            if clienti_selezionati:
                agg = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                agg['Q.ta'] = agg['QTA_DEFAULT'].astype(int)
                agg = agg[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, agg], ignore_index=True)
                st.session_state.giro_corrente = st.session_state.giro_corrente.sort_values(by="POSIZIONE").reset_index(drop=True)
                st.success("Aggiunti al giro! Passaggio automatico al giro...")
                st.session_state.pagina_attiva = "giro"
                st.rerun()
            else:
                st.warning("Seleziona almeno un cliente.")
                
        with st.expander("👀 Visualizza o Modifica Anagrafica Clienti intera"):
            edited_db = st.data_editor(
                st.session_state.db_clienti,
                num_rows="dynamic",
                use_container_width=True,
                key="db_editor_switch"
            )
            st.session_state.db_clienti = edited_db
    else:
        st.warning("Nessun cliente trovato. Assicurati che il file 'database.xlsx' sia stato caricato su GitHub.")
