import streamlit as st
import pandas as pd
import os

st.set_page_config(page_title="VanGo - Gestione Consegne e Clienti", page_icon="🚐", layout="wide")

# Inizializzazione dello stato della sessione
if "db_clienti" not in st.session_state:
    st.session_state.db_clienti = pd.DataFrame()
if "clienti_selezionati_m" not in st.session_state:
    st.session_state.clienti_selezionati_m = []

def elabora_dataframe_db(df):
    # Pulisci eventuali spazi vuoti nelle stringhe e righe vuote
    df = df.dropna(how="all")
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].astype(str).str.strip()
    return df

def carica_database_iniziale():
    # Cerca un file di default sul server
    if os.path.exists("database.xlsx"):
        try:
            df = pd.read_excel("database.xlsx")
            return elabora_dataframe_db(df)
        except Exception:
            pass
    elif os.path.exists("database.csv"):
        try:
            df = pd.read_csv("database.csv")
            return elabora_dataframe_db(df)
        except Exception:
            pass
    
    # DataFrame di fallback se non esiste nulla
    return pd.DataFrame(columns=["ID", "Cliente", "Indirizzo", "Città", "CAP", "Note"])

# Caricamento iniziale se vuoto
if st.session_state.db_clienti.empty:
    st.session_state.db_clienti = carica_database_iniziale()

st.title("🚐 VanGo - Gestione Consegne & Clienti")
st.markdown("---")

# Sidebar per la gestione del Database e File Uploader
with st.sidebar:
    st.header("📂 Gestione Database")
    
    # Uploader del file
    caricamento_file = st.file_uploader("Carica Database (Excel o CSV)", type=["xlsx", "csv"])
    
    if caricamento_file is not None:
        try:
            # Salviamo fisicamente il file caricato come database predefinito sul server
            if caricamento_file.name.endswith('.csv'):
                df_up = pd.read_csv(caricamento_file)
                df_up.to_excel("database.xlsx", index=False)
            else:
                with open("database.xlsx", "wb") as f:
                    f.write(caricamento_file.getbuffer())
                df_up = pd.read_excel(caricamento_file)
            
            st.session_state.db_clienti = elabora_dataframe_db(df_up)
            st.session_state.clienti_selezionati_m = []
            
            st.success(f"Database caricato e salvato! ({len(st.session_state.db_clienti)} clienti)")
            st.rerun()
        except Exception as e:
            st.error(f"Errore nella lettura del file: {e}")

    st.markdown("---")
    
    # Tasto Reset / Ricarica DB da zero
    if st.button("🔄 Reset / Ricarica da Server", use_container_width=True):
        st.session_state.db_clienti = carica_database_iniziale()
        st.session_state.clienti_selezionati_m = []
        st.success(f"Database ricaricato dal server! Totale: {len(st.session_state.db_clienti)} clienti.")
        st.rerun()

    st.markdown("### Info Database")
    tot_clienti = len(st.session_state.db_clienti)
    st.metric(label="Clienti Totali nel DB", value=tot_clienti)

# Sezione Principale: Ricerca e Visualizzazione Clienti
st.subheader("🔍 Ricerca e Gestione Clienti")

if not st.session_state.db_clienti.empty:
    # Barra di ricerca
    query_ricerca = st.text_input("Cerca cliente per nome, indirizzo o città:", "")
    
    df_vis = st.session_state.db_clienti.copy()
    
    if query_ricerca:
        # Filtro case-insensitive su tutte le colonne stringa
        mask = df_vis.astype(str).apply(lambda x: x.str.contains(query_ricerca, case=False, na=False)).any(axis=1)
        df_vis = df_vis[mask]
    
    st.write(f"Visualizzazione di **{len(df_vis)}** clienti trovati (su {len(st.session_state.db_clienti)} totali):")
    
    # Tabella interattiva dei clienti
    st.dataframe(df_vis, use_container_width=True, hide_index=True)
    
    st.markdown("---")
    st.subheader("➕ Aggiungi Rapido Nuovo Cliente")
    
    with st.form("form_aggiungi_cliente", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            nuovo_nome = st.text_input("Nome Cliente / Azienda")
            nuovo_indirizzo = st.text_input("Indirizzo")
        with col2:
            nuova_citta = st.text_input("Città")
            nuovo_cap = st.text_input("CAP")
            
        nuove_note = st.text_input("Note (es. orari scarico, referenti)")
        
        submit_aggiungi = st.form_submit_button("Registra Nuovo Cliente nel DB")
        
        if submit_aggiungi:
            if nuovo_nome:
                nuovo_id = len(st.session_state.db_clienti) + 1
                
                nuova_riga = {
                    "ID": nuovo_id,
                    "Cliente": nuovo_nome.strip(),
                    "Indirizzo": nuovo_indirizzo.strip(),
                    "Città": nuova_citta.strip(),
                    "CAP": nuovo_cap.strip(),
                    "Note": nuove_note.strip()
                }
                
                # Aggiungiamo al dataframe in sessione
                st.session_state.db_clienti = pd.concat([st.session_state.db_clienti, pd.DataFrame([nuova_riga])], ignore_index=True)
                
                # Salviamo immediatamente su file Excel sul server per renderlo persistente
                st.session_state.db_clienti.to_excel("database.xlsx", index=False)
                
                st.success(f"Cliente '{nuovo_nome}' aggiunto con successo! Totale aggiornato a {len(st.session_state.db_clienti)} clienti.")
                st.rerun()
            else:
                st.warning("Inserisci almeno il nome del cliente.")
else:
    st.info("Nessun database caricato. Carica un file Excel o CSV dalla barra laterale per iniziare.")
