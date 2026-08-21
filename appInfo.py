import streamlit as st
import pandas as pd
import urllib.parse

# Configurazione Pagina
st.set_page_config(
    page_title="Gestione Giro Consegne",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Styling CSS
st.markdown("""
<style>
    .main-header {
        font-size: 24px;
        font-weight: bold;
        color: #1E3A8A;
        text-align: center;
        padding: 10px;
        background-color: #E2E8F0;
        border-radius: 8px;
        margin-bottom: 15px;
    }
    .stButton>button {
        width: 100%;
        font-weight: bold;
        border-radius: 6px;
    }
</style>
""", unsafe_allow_html=True)

# Inizializzazione Database Clienti
if 'db_clienti' not in st.session_state:
    st.session_state.db_clienti = pd.DataFrame([
        {"ZONA": 100, "CLIENTE": "IL LATO DOLCE", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Via Fratelli Di Dio 15", "ORA": "05:30", "QTA_DEFAULT": 5},
        {"ZONA": 100, "CLIENTE": "MOUSSA MEDHAT", "COMUNE": "MILANO", "VIA": "Viale Monza 315", "ORA": "05:30", "QTA_DEFAULT": 5},
        {"ZONA": 100, "CLIENTE": "FORTUNATA", "COMUNE": "MILANO", "VIA": "Aldi viale monza milano", "ORA": "06:30", "QTA_DEFAULT": 2},
        {"ZONA": 100, "CLIENTE": "ALPI MULTIMEDICA", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Via Milanese 300", "ORA": "06:00", "QTA_DEFAULT": 6},
        {"ZONA": 100, "CLIENTE": "EL PROFESOR", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Piazza Iv Novembre", "ORA": "06:30", "QTA_DEFAULT": 10},
        {"ZONA": 100, "CLIENTE": "CeC", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Piazza Petazzi 20", "ORA": "06:30", "QTA_DEFAULT": 7},
        {"ZONA": 100, "CLIENTE": "PROJECT HL", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Viale Italia 555", "ORA": "07:00", "QTA_DEFAULT": 7},
        {"ZONA": 100, "CLIENTE": "IL FORNO MAGICO manzoni", "COMUNE": "PADERNO DUGNANO", "VIA": "Via Monte Sabotino, 34", "ORA": "01:00", "QTA_DEFAULT": 9},
        {"ZONA": 200, "CLIENTE": "PEQUINI BALLKIZE", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Via Fogagnolo 165", "ORA": "06:30", "QTA_DEFAULT": 0},
        {"ZONA": 200, "CLIENTE": "BAR L'ARPA", "COMUNE": "SESTO SAN GIOVANNI", "VIA": "Via Fogagnolo 123", "ORA": "06:30", "QTA_DEFAULT": 0},
    ])

# Inizializzazione Giro Corrente
if 'giro_corrente' not in st.session_state:
    st.session_state.giro_corrente = pd.DataFrame(columns=['CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Sidebar Menu
st.sidebar.title("📌 Menu")
page = st.sidebar.radio("Seleziona Schermata", ["Pianificazione Giro", "Database Clienti"])

if page == "Pianificazione Giro":
    st.markdown("<div class='main-header'>PIANIFICAZIONE GIRO CONSEGNE</div>", unsafe_allow_html=True)
    
    tot_clienti = len(st.session_state.giro_corrente)
    tot_qta = st.session_state.giro_corrente['Q.ta'].sum() if not st.session_state.giro_corrente.empty else 0
    
    col_stat1, col_stat2, col_stat3 = st.columns([1, 1, 2])
    col_stat1.metric(label="N° Clienti", value=tot_clienti)
    col_stat2.metric(label="Q.tà Totale Merce", value=tot_qta)
    col_stat3.date_input("Data Giro Consegne")

    st.subheader("📋 Lista Consegne del Giorno")
    
    if not st.session_state.giro_corrente.empty:
        edited_df = st.data_editor(
            st.session_state.giro_corrente,
            column_config={
                "Q.ta": st.column_config.NumberColumn("Q.tà", min_value=0, step=1),
                "CLIENTE": st.column_config.TextColumn("Cliente", disabled=True),
                "COMUNE": st.column_config.TextColumn("Comune", disabled=True),
                "VIA": st.column_config.TextColumn("Via / Indirizzo", disabled=True),
                "ORA": st.column_config.TextColumn("Ora Consegna"),
            },
            num_rows="dynamic",
            use_container_width=True,
            key="giro_editor"
        )
        st.session_state.giro_corrente = edited_df
    else:
        st.info("Il giro è attualmente vuoto. Vai nel 'Database Clienti' per selezionare ed aggiungere i clienti al giro.")

    st.markdown("---")
    st.subheader("⚡ Azioni Rapide")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        if st.button("🔄 INVERTI GIRO"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                st.rerun()

    with col2:
        if st.button("⏰ OTTIMIZZA ORA"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = st.session_state.giro_corrente.sort_values(by="ORA").reset_index(drop=True)
                st.rerun()

    with col3:
        if st.button("🗺️ APRI SU MAPS"):
            if not st.session_state.giro_corrente.empty:
                addresses = [f"{row['VIA']}, {row['COMUNE']}" for _, row in st.session_state.giro_corrente.iterrows()]
                if len(addresses) == 1:
                    maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(addresses[0])}"
                else:
                    origin = urllib.parse.quote(addresses[0])
                    destination = urllib.parse.quote(addresses[-1])
                    waypoints = "|".join([urllib.parse.quote(addr) for addr in addresses[1:-1]])
                    maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"
                st.markdown(f"[👉 Clicca qui per aprire il percorso su Google Maps]({maps_url})", unsafe_allow_html=True)

    with col4:
        if st.button("🗑️ CANCELLA GIRO"):
            st.session_state.giro_corrente = pd.DataFrame(columns=['CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
            st.rerun()

    with col5:
        if st.button("🔄 RESET QUANTITÀ"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente['Q.ta'] = 0
                st.rerun()

elif page == "Database Clienti":
    st.markdown("<div class='main-header'>DATABASE CLIENTI ANAGRAFICA</div>", unsafe_allow_html=True)

    st.subheader("➕ Aggiungi Nuovo Cliente nel Database")
    with st.form("nuovo_cliente_form"):
        col_f1, col_f2, col_f3 = st.columns(3)
        zona = col_f1.number_input("Zona", value=100)
        cliente = col_f1.text_input("Cliente")
        comune = col_f2.text_input("Comune")
        via = col_f2.text_input("Via e Civico")
        ora = col_f3.text_input("Orario Consegna", value="06:00")
        qta_def = col_f3.number_input("Quantità Predefinita", value=0)
        
        if st.form_submit_button("Salva in Anagrafica") and cliente and via:
            new_row = {"ZONA": zona, "CLIENTE": cliente, "COMUNE": comune, "VIA": via, "ORA": ora, "QTA_DEFAULT": qta_def}
            st.session_state.db_clienti = pd.concat([st.session_state.db_clienti, pd.DataFrame([new_row])], ignore_index=True)
            st.success(f"Cliente {cliente} salvato in Anagrafica!")

    st.markdown("---")
    st.subheader("📁 Anagrafica Completa")
    edited_db = st.data_editor(
        st.session_state.db_clienti,
        num_rows="dynamic",
        use_container_width=True,
        key="db_editor"
    )
    st.session_state.db_clienti = edited_db

    st.markdown("---")
    st.subheader("🚚 Aggiungi Clienti Selezionati al Giro Consegne")
    
    lista_clienti = st.session_state.db_clienti['CLIENTE'].dropna().tolist()
    
    clienti_selezionati = st.multiselect(
        "Seleziona i clienti da inserire nel giro del giorno:",
        options=lista_clienti
    )
    
    if st.button("➕ AGGIUNGI SELEZIONATI AL GIRO"):
        if clienti_selezionati:
            clienti_da_agg = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
            clienti_da_agg['Q.ta'] = clienti_da_agg['QTA_DEFAULT']
            clienti_da_agg = clienti_da_agg[['CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
            
            st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, clienti_da_agg], ignore_index=True)
            st.success(f"{len(clienti_selezionati)} clienti aggiunti al giro corrente!")
        else:
            st.warning("Seleziona almeno un cliente prima di premere il pulsante.")
