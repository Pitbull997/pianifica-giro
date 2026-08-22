import streamlit as st
import pandas as pd
import urllib.parse
import os
import json
import base64

# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

FILE_GIRO_PERSISTENTE = "giro_salvato.json"

# CSS Avanzato - Forzatura Dark Mode & Fix UI Mobile + Overlay al 90%
st.markdown("""
<style>
    .stApp, body, html {
        background-color: #121212 !important;
        color: #FFFFFF !important;
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
        height: 52px !important;
        font-size: 15px !important;
    }

    .btn-inactive div[data-testid="stButton"] > button {
        background-color: #1E293B !important;
        color: #94A3B8 !important;
        border: 1px solid #334155 !important;
        height: 52px !important;
        font-size: 15px !important;
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

    /* Stili per l'immagine responsive e il pulsante sovrapposto al 90% */
    .hero-container {
        position: relative;
        width: 100%;
        max-width: 450px;
        margin: 0 auto;
    }
    .hero-img {
        width: 100%;
        height: auto;
        display: block;
        border-radius: 12px;
    }
    .hero-btn {
        position: absolute;
        top: 90%;
        left: 50%;
        transform: translate(-50%, -50%);
        background-color: #2563EB !important;
        color: white !important;
        padding: 14px 20px;
        border-radius: 30px;
        font-weight: bold;
        text-decoration: none !important;
        box-shadow: 0 4px 15px rgba(37, 99, 235, 0.7);
        text-align: center;
        width: 82%;
        font-size: 16px;
        border: 2px solid #60A5FA !important;
        display: block;
        z-index: 10;
    }
    .hero-btn:hover {
        background-color: #1D4ED8 !important;
        color: white !important;
    }
</style>
""", unsafe_allow_html=True)

# Funzione per pulire formato orario
def pulisci_orario(valore):
    val_str = str(valore).strip()
    if 'days' in val_str:
        val_str = val_str.split()[-1]
    if len(val_str) >= 5:
        return val_str[:5]
    return val_str

# Carica Database iniziale
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

# Gestione navigazione tramite parametri URL
if "nav" in st.query_params and st.query_params["nav"] == "giro":
    st.session_state.pagina_attiva = "giro"
    st.query_params.clear()

# Inizializzazione sessioni
if 'db_clienti' not in st.session_state or st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_db_predefinito()

if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = carica_giro_da_disco()

if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "welcome"

if 'clienti_sequenza' not in st.session_state:
    st.session_state.clienti_sequenza = []

if 'temp_qta_seq' not in st.session_state:
    st.session_state.temp_qta_seq = {}

# ==========================================
# SCHERMATA 0: WELCOME / HOME PAGE GRAFICA
# ==========================================
if st.session_state.pagina_attiva == "welcome":
    img_path = "vango_splash.png"
    if os.path.exists(img_path):
        with open(img_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode()
        
        st.markdown(f"""
            <div class="hero-container">
                <img src="data:image/png;base64,{encoded_string}" class="hero-img">
                <a href="?nav=giro" target="_self" class="hero-btn">ENTRA IN VanGo</a>
            </div>
        """, unsafe_allow_html=True)
    else:
        st.warning("⚠️ Immagine 'vango_splash.png' non trovata nella cartella.")
        if st.button("ENTRA IN VanGo", use_container_width=True, type="primary"):
            st.session_state.pagina_attiva = "giro"
            st.rerun()

# ==========================================
# APPLICAZIONE PRINCIPALE
# ==========================================
else:
    if st.button("🏠 Home Grafica", key="btn_home_grafica"):
        st.session_state.pagina_attiva = "welcome"
        st.rerun()

    st.markdown("<h1 style='text-align: center; color: #FFFFFF; font-size: 26px; margin-bottom: 20px;'>🚐 VANGO</h1>", unsafe_allow_html=True)

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
        if st.button("📁 INSERISCI CLIENTE", use_container_width=True, key="btn_db"):
            st.session_state.pagina_attiva = "db"
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
            st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
            
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
        else:
            st.info("Nessuna fermata nel giro corrente. Clicca in alto su 'INSERISCI CLIENTE' per aggiungerne.")

    # ==========================================
    # SCHERMATA 2: INSERISCI CLIENTE IN SEQUENZA
    # ==========================================
    elif st.session_state.pagina_attiva == "db":
        st.subheader("📁 Inserisci Clienti in Sequenza")
        
        if not st.session_state.db_clienti.empty:
            # Funzione richiamata automaticamente al click/selezione nel menu a tendina
            def aggiungi_da_tendina():
                cli = st.session_state.select_singolo_cliente
                if cli and cli != "-- Seleziona cliente --" and cli not in st.session_state.clienti_sequenza:
                    st.session_state.clienti_sequenza.append(cli)
                    default_val = int(st.session_state.db_clienti.loc[st.session_state.db_clienti['CLIENTE'] == cli, 'QTA_DEFAULT'].values[0])
                    st.session_state.temp_qta_seq[cli] = default_val
                st.session_state.select_singolo_cliente = "-- Seleziona cliente --"

            clienti_disponibili = [
                c for c in st.session_state.db_clienti['CLIENTE'].dropna().tolist() 
                if c not in st.session_state.clienti_sequenza
            ]
            
            st.markdown("Seleziona i clienti **uno alla volta** dal menu a tendina. Cliccando un cliente si aggiunge subito alla lista:")
            
            st.selectbox(
                "Scegli cliente:", 
                options=["-- Seleziona cliente --"] + clienti_disponibili,
                key="select_singolo_cliente",
                on_change=aggiungi_da_tendina
            )

            # Mostriamo la sequenza dei clienti scelti fino a questo momento con i rispettivi campi colli
            if st.session_state.clienti_sequenza:
                st.markdown("---")
                st.markdown("### 📋 Clienti in coda (inserisci i colli per ciascuno):")
                
                clienti_da_rimuovere = []
                
                for idx, cli in enumerate(st.session_state.clienti_sequenza):
                    col_info, col_qta, col_del = st.columns([2, 1, 0.5])
                    
                    with col_info:
                        st.markdown(f"**{idx + 1}. {cli}**")
                        
                    with col_qta:
                        val_attuale = st.session_state.temp_qta_seq.get(cli, 0)
                        st.session_state.temp_qta_seq[cli] = st.number_input(
                            "Colli", 
                            min_value=0, 
                            value=val_attuale, 
                            key=f"qta_seq_{cli}"
                        )
                        
                    with col_del:
                        st.write("")
                        if st.button("❌", key=f"del_seq_{cli}"):
                            clienti_da_rimuovere.append(cli)

                # Rimuoviamo eventuali clienti scartati dall'utente
                if clienti_da_rimuovere:
                    for cli in clienti_da_rimuovere:
                        st.session_state.clienti_sequenza.remove(cli)
                        if cli in st.session_state.temp_qta_seq:
                            del st.session_state.temp_qta_seq[cli]
                    st.rerun()

                st.markdown("---")
                if st.button("🚀 AGGIUNGI AL GIRO DEFINITIVO", use_container_width=True, type="primary"):
                    nuovi_clienti = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(st.session_state.clienti_sequenza)].copy()
                    
                    nuovi_clienti['temp_idx'] = nuovi_clienti['CLIENTE'].map({c: i for i, c in enumerate(st.session_state.clienti_sequenza)})
                    nuovi_clienti = nuovi_clienti.sort_values('temp_idx').drop(columns=['temp_idx'])
                    
                    nuovi_clienti['Q.ta'] = nuovi_clienti['CLIENTE'].map(st.session_state.temp_qta_seq)
                    nuovi_clienti = nuovi_clienti[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                    
                    st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, nuovi_clienti], ignore_index=True)
                    st.session_state.giro_corrente['POSIZIONE'] = range(1, len(st.session_state.giro_corrente) + 1)
                    
                    salva_giro_su_disco(st.session_state.giro_corrente)
                    st.session_state.clienti_sequenza = []
                    st.session_state.temp_qta_seq = {}
                    
                    st.success("Giro aggiornato con successo!")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()

            st.markdown("---")
            with st.expander("👀 Visualizza o Modifica Anagrafica Clienti intera"):
                edited_db = st.data_editor(
                    st.session_state.db_clienti,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="db_editor_switch"
                )
                st.session_state.db_clienti = edited_db
        else:
            st.warning("Nessun cliente trovato. Verifica che il file del database sia caricato.")
