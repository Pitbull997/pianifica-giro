else:
            # Vista Tabella con Selectbox per riordinare
            for idx in range(tot_clienti):
                row = st.session_state.giro_corrente.iloc[idx]
                
                st.markdown(f"<h3 style='margin: 0; padding-top: 4px; font-size: 18px;'>{idx + 1}. {row['CLIENTE']}</h3>", unsafe_allow_html=True)
                st.markdown(f"<div style='color: #A3A3A3; font-size: 14px; margin-top: 4px; margin-bottom: 8px;'>📍 {row['VIA']}, {row['COMUNE']}</div>", unsafe_allow_html=True)
                
                col_c1, col_c2 = st.columns([1, 1])
                
                with col_c1:
                    # Selectbox per spostare la posizione
                    nuova_pos = st.selectbox(
                        "Sposta a pos:",
                        options=[i for i in range(1, tot_clienti + 1)],
                        index=idx,
                        key=f"select_tbl_{idx}",
                        label_visibility="collapsed"
                    )
                    
                    if nuova_pos - 1 != idx:
                        df_temp = st.session_state.giro_corrente.copy()
                        riga = df_temp.iloc[idx]
                        
                        df_temp = df_temp.drop(df_temp.index[idx])
                        top = df_temp.iloc[:nuova_pos - 1]
                        bottom = df_temp.iloc[nuova_pos - 1:]
                        
                        df_nuovo = pd.concat([top, pd.DataFrame([riga]), bottom], ignore_index=True)
                        df_nuovo['POSIZIONE'] = range(1, len(df_nuovo) + 1)
                        
                        st.session_state.giro_corrente = df_nuovo
                        salva_giro_su_disco(st.session_state.giro_corrente)
                        st.rerun()

                with col_c2:
                    nuova_qta = st.number_input(
                        "Q.tà",
                        min_value=0,
                        value=int(row['Q.ta']),
                        key=f"qta_inp_{idx}",
                        label_visibility="collapsed"
                    )
                    if nuova_qta != int(row['Q.ta']):
                        st.session_state.giro_corrente.at[idx, 'Q.ta'] = nuova_qta
                        salva_giro_su_disco(st.session_state.giro_corrente)
                        st.rerun()

                st.markdown("<hr style='margin: 12px 0; border-color: #262626;'>", unsafe_allow_html=True)
