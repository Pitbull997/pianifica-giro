import streamlit as st
import pandas as pd
import urllib.parse
import os
import json
import base64

# Configurazione Pagina
st.set_page_config(page_title="Giro Consegne", page_icon="🚚", layout="wide", initial_sidebar_state="collapsed")

FILE_GIRO_PERSISTENTE = "giro_salvato.json"

def get_base64_of_bin_file(bin_file):
    with open(bin_file, 'rb') as f:
        data = f.read()
    return base64.b64encode(data).decode()

bg_css = ""
if os.path.exists("sfondo.jpg"):
    bin_str = get_base64_of_bin_file("sfondo.jpg")
    bg_css = f".stApp {{ background-image: linear-gradient(rgba(11, 15, 25, 0.88), rgba(11, 15, 25, 0.94)), url('data:image/jpeg;base64,{bin_str}'); background-size: cover; background-position: center; }}"

st.markdown(f"""
<style>
    {bg_css}
    .stApp {{ background-color: #0B0F19 !important; color: #FFFFFF !important; }}
    .stop-card {{ background: rgba(22, 30, 46, 0.9); border-left: 5px solid #2563EB; padding: 12px; border-radius: 10px; margin-top: 10px; border: 1px solid #334155; }}
    .btn-active div[data-testid="stButton"] > button {{ background: #2563EB !important; color: white !important; }}
    .btn-inactive div[data-testid="stButton"] > button {{ background: #1E293B !important; color: #94A3B8 !important; }}
</style>
""", unsafe_allow_html=True)

def pulisci_orario(valore):
    val_str = str(valore).strip()
    return val_str[:5] if len(val_str) >= 5 else val_str

def carica_db_predefinito():
    if os.path.exists("database.csv"): 
        df = pd.read_csv("database.csv")
        df.columns = df.columns.str.strip().str.upper()
        return df
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT'])

def salva_giro_su_disco(df):
    df.to_json(FILE_GIRO_PERSISTENTE, orient="records", date_format="iso")

def carica_giro_da_disco():
    if os.path.exists(FILE_GIRO_PERSISTENTE):
        return pd.read_json(FILE_GIRO_PERSISTENTE, orient="records")
    return pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

# Stato sessione
if 'db_clienti' not in st.session_state: st.session_state.db_clienti = carica_db_predefinito()
if 'giro_corrente' not in st.session_state: st.session_state.giro_corrente = carica_giro_da_disco()
if 'pagina_attiva' not in st.session_state: st.session_state.pagina_attiva = "giro"

# Navigazione
col_sw1, col_sw2 = st.columns(2)
with col_sw1:
    if st.button("📍 GIRO DEL GIORNO", use_container_width=True): st.session_state.pagina_attiva = "giro"; st.rerun()
with col_sw2:
    if st.button("📁 DATABASE CLIENTI", use_container_width=True): st.session_state.pagina_attiva = "db"; st.rerun()

# Logica Giro
if st.session_state.pagina_attiva == "giro":
    st.subheader("🗺️ Giro Consegne")
    if not st.session_state.giro_corrente.empty:
        vista = st.radio("Modalità vista:", ["📱 Lista Schede", "✏️ Tabella Modificabile"], horizontal=True)
        tot_c = len(st.session_state.giro_corrente)
        
        for idx in range(tot_c):
            row = st.session_state.giro_corrente.iloc[idx]
            if vista == "📱 Lista Schede":
                st.markdown(f"<div class='stop-card'><b>{idx+1}. {row['CLIENTE']}</b><br>{row['VIA']}, {row['COMUNE']}</div>", unsafe_allow_html=True)
            else:
                st.write(f"### {idx+1}. {row['CLIENTE']}")
            
            nuova_pos = st.selectbox("Sposta a pos:", options=range(1, tot_c + 1), index=idx, key=f"pos_{idx}", label_visibility="collapsed")
            if nuova_pos - 1 != idx:
                df = st.session_state.giro_corrente.copy()
                riga = df.iloc[idx]
                df = df.drop(df.index[idx])
                top = df.iloc[:nuova_pos - 1]
                bot = df.iloc[nuova_pos - 1:]
                st.session_state.giro_corrente = pd.concat([top, pd.DataFrame([riga]), bot], ignore_index=True)
                salva_giro_su_disco(st.session_state.giro_corrente); st.rerun()
    else:
        st.info("Giro vuoto. Vai in Database per aggiungere clienti.")

elif st.session_state.pagina_attiva == "db":
    st.subheader("📁 Database")
    clienti = st.multiselect("Seleziona clienti:", st.session_state.db_clienti['CLIENTE'].tolist())
    if st.button("Aggiungi al giro"):
        agg = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti)].copy()
        agg['Q.ta'] = agg['QTA_DEFAULT']
        st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, agg], ignore_index=True)
        salva_giro_su_disco(st.session_state.giro_corrente); st.rerun()
