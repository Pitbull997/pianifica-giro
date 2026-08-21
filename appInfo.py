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

# CSS Avanzato per UI Mobile Perfetta & Alto Contrasto
st.markdown("""
<style>
    /* Sfondo generale scuro */
    .stApp {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }

    /* Testi e Metric con alto contrasto */
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

    /* Pulsante Attivo (Azzurro/Blu Intenso) */
    .btn-active button {
        background-color: #2563EB !important;
        color: #FFFFFF !important;
        border: 2px solid #60A5FA !important;
        border-radius: 12px !important;
        height: 52px !important;
        font-weight: bold !important;
        font-size: 14px !important;
        box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4) !important;
    }

    /* Pulsante Non Attivo (Grigio Scuro ben visibile) */
    .btn-inactive button {
        background-color: #1E293B !important;
        color: #CBD5E1 !important;
        border: 1px solid #475569 !important;
        border-radius: 12px !important;
        height: 52px !important;
        font-weight: bold !important;
        font-size: 14px !important;
    }

    /* Card fermata stile Timeline */
    .stop-card {
        background-color: #1E1E1E;
        border-left: 5px solid #2563EB;
        padding: 12px 14px;
        border-radius: 10px 10px 0 0;
        margin-top: 10px;
        border-top: 1px solid #334155;
        border-right: 1px solid #334155;
    }
    .stop-title {
        font-size: 17px;
        font-weight: bold;
        color: #FFFFFF;
        margin-bottom: 4px;
    }
    .stop-address {
        font-size: 14px;
        color: #E2E8F0;
        margin-bottom: 6px;
    }
    .stop-meta {
        font-size: 13px;
        color: #60A5FA;
        font-weight: 600;
    }

    /* Box inferiore controllo posizione */
    .stop-control-box {
        background-color: #161E2E;
        border-left: 5px solid #2563EB;
        border-bottom: 1px solid #334155;
        border-right: 1px solid #334155;
        border-radius: 0 0 10px 10px;
        padding: 8px 12px;
        margin-bottom: 12px;
    }

    /* Modifica stile expander */
    div[data-testid="stExpander"] {
        background-color: #1E1E1E !important;
        border-radius: 10px !important;
        border: 1px solid #334155 !important;
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
# SWITCHER PULSANTI IN ALTO
# ==========================================
col_sw1, col_sw2 = st.columns(2)

with col_sw1:
    css_class = "btn-active" if st.session_state.pagina_attiva == "giro" else "btn-inactive"
    st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
    if st.button("📍 GIRO DEL GIORNO", use_container_width=True, key="btn_giro"):
        st.session_state.pagina_attiva = "giro"
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

with col_sw2:
    css_class = "btn-active" if st.session_state.pagina_attiva == "db" else "btn-inactive"
    st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
    if st.button("📁 DATABASE CLIENTI", use_container_width=True, key="btn_db"):
        st.session_state.pagina_attiva = "db"
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

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
            # Assegna una numerazione sequenziale se manca
            st.session_state.giro_corrente['POSIZIONE'] = range(1, len(st.session_state.giro_corrente) + 1)
            
            for idx, row in st.session_state.giro_corrente.iterrows():
                # Scheda dati cliente
                st.markdown(f"""
                <div class="stop-card">
                    <div class="stop-title">{int(row['POSIZIONE'])}. {row['CLIENTE']}</div>
                    <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                    <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
                </div>
                """, unsafe_allow_html=True)

                # Controlli rapidi sotto la scheda (Cambio Ordine + Naviga)
                col_c1, col_c2 = st.columns([1, 1])
                with col_c1:
                    nuova_pos = st.selectbox(
                        "Sposta alla pos:",
                        options=list(range(1, tot_clienti + 1)),
                        index=int(row['POSIZIONE']) - 1,
                        key=f"pos_{idx}"
                    )
                    # Se l'utente cambia il numero della posizione
                    if nuova_pos != int(row['POSIZIONE']):
                        st.session_state.giro_corrente.at[idx, 'POSIZIONE'] = nuova_pos - 0.5
                        st.session_state.giro_corrente = st.session_state.giro_corrente.sort_values(by="POSIZIONE").reset_index(drop=True)
                        st.rerun()

                with col_c2:
                    st.write("") # Spaziatore
                    dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
                    st.markdown(f"[🚘 **NAVIGA ORA**](https://www.google.com/maps/dir/?api=1&destination={dest})")

        else:
            edited_df = st.data_editor(
                st.session_state.giro_corrente,
                column_config={
                    "POSIZIONE": st.column_config.NumberColumn("Pos.", min_value=1, format="%d"),
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
            # Riordina in base alla posizione inserita a mano
            st.session_state.giro_corrente = edited_df.sort_values(by="POSIZIONE").reset_index(drop=True)

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
                <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
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
                st.success("Aggiunti al giro! Passaggio automatico...")
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
