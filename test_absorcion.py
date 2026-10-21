import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
from unittest.mock import MagicMock

from engine import AxiomEngine
from topstep_client import TopstepClient

class TestAbsorcionVWAP(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp_csv = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        self.temp_csv.close()
        
        self.mock_client = MagicMock(spec=TopstepClient)
        self.engine = AxiomEngine(self.mock_client, default_symbol="NQ")
        self.engine.csv_path = self.temp_csv.name
        self.engine.symbol_states["NQ"]["tick_size"] = 0.25
        self.engine.symbol_states["NQ"]["contract_id"] = "CON_NQ_TEST"
        self.engine.symbol_states["ES"]["tick_size"] = 0.25
        self.engine.symbol_states["ES"]["contract_id"] = "CON_ES_TEST"
        self.engine.account_id = 12345
        
        # Base 5m bar with VWAP at 20000.0, Upper_Band at 20040.0, Lower_Band at 19960.0
        self.now = datetime.now(timezone.utc)
        self.df_5m = pd.DataFrame([{
            'Timestamp': self.now,
            'Open': 20010.0,
            'High': 20015.0,
            'Low': 19999.5,   # Entered zone (+-5 ticks of 20000.0 is 19998.75 to 20001.25)
            'Close': 20002.0,
            'Volume': 5000,
            'VWAP': 20000.0,
            'Upper_Band': 20040.0,
            'Lower_Band': 19960.0
        }])

    def tearDown(self):
        import os
        if hasattr(self, 'temp_csv') and os.path.exists(self.temp_csv.name):
            try:
                os.remove(self.temp_csv.name)
            except Exception:
                pass

    def _crear_df_1m(self, vol_cerrada=1150, open_p=20002.0, high_p=20003.0, low_p=19999.5, close_p=20001.5):
        bars = []
        base_time = self.now - timedelta(minutes=15)
        # 10 closed bars with 1000 volume each
        for i in range(10):
            bars.append({
                'Timestamp': base_time + timedelta(minutes=i),
                'Open': 20010.0,
                'High': 20012.0,
                'Low': 20008.0,
                'Close': 20010.0,
                'Volume': 1000
            })
        # 11th bar: The closed bar being evaluated
        bars.append({
            'Timestamp': base_time + timedelta(minutes=10),
            'Open': open_p,
            'High': high_p,
            'Low': low_p,
            'Close': close_p,
            'Volume': vol_cerrada
        })
        # 12th bar: The current partial/forming bar
        bars.append({
            'Timestamp': base_time + timedelta(minutes=11),
            'Open': close_p,
            'High': close_p + 1.0,
            'Low': close_p - 0.5,
            'Close': close_p + 0.25,
            'Volume': 50
        })
        return pd.DataFrame(bars)

    def test_long_absorcion_detectada(self):
        """Prueba setup LONG: Vol +15%, SL a 5 ticks del extremo, TP dinámico por volumen."""
        df_1m = self._crear_df_1m(
            vol_cerrada=1150,  # 15% arriba del promedio (1000)
            open_p=20002.0,
            high_p=20003.0,
            low_p=19999.50,   # Entró a zona VWAP (+-5 ticks)
            close_p=20001.50   # Cerró por encima de VWAP y mecha inf = 2.0 pts (8 ticks >= 2 ticks)
        )
        setup = self.engine.verificar_absorcion_vwap_1m(self.df_5m, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "LONG")
        self.assertEqual(setup["extremo_precio"], 19999.50)
        self.assertEqual(setup["volumen_1m"], 1150)
        self.assertGreaterEqual(setup["rvol"], 1.10)

        # SL a 10 ticks de la línea VWAP (20000.0 - 2.50 = 19997.50)
        # TP al 60% de la distancia entre VWAP y Upper_Band (40.0 pts * 0.60 = 24.0 pts = 96 ticks)
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20001.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0, rvol=setup["rvol"]
        )
        self.assertEqual(precio_sl, 19997.50)  # 20000.0 - 10 * 0.25 = 19997.50 (SL a 10 ticks de VWAP)
        self.assertEqual(ticks_sl, -16)        # (20001.50 - 19997.50) / 0.25 = 16 ticks
        self.assertEqual(ticks_tp, 96)         # 96 ticks por 60% de la banda (24.0 pts)
        self.assertEqual(precio_tp, 20025.50)

    def test_short_absorcion_detectada(self):
        """Prueba setup SHORT: SL a 10 ticks de VWAP, TP al 60% de la banda inferior."""
        df_5m_short = pd.DataFrame([{
            'Timestamp': self.now,
            'Open': 19990.0,
            'High': 20000.5,   # En zona VWAP
            'Low': 19985.0,
            'Close': 19998.0,
            'Volume': 5000,
            'VWAP': 20000.0,
            'Upper_Band': 20040.0,
            'Lower_Band': 19960.0
        }])
        df_1m = self._crear_df_1m(
            vol_cerrada=1200,  # 20% arriba del promedio (1000)
            open_p=19998.0,
            high_p=20000.50,   # Entró a zona VWAP (+-5 ticks)
            low_p=19997.0,
            close_p=19998.50   # Cerró por debajo de VWAP y mecha sup = 2.0 pts
        )
        setup = self.engine.verificar_absorcion_vwap_1m(df_5m_short, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "SHORT")
        self.assertEqual(setup["extremo_precio"], 20000.50)
        self.assertEqual(setup["volumen_1m"], 1200)

        # SL a 10 ticks de la línea VWAP (20000.0 + 2.50 = 20002.50)
        # TP al 60% de la banda inferior (40.0 pts * 0.60 = 24.0 pts = 96 ticks)
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "SHORT", 19998.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0, rvol=setup["rvol"]
        )
        self.assertEqual(precio_sl, 20002.50)  # 20000.0 + 10 * 0.25 = 20002.50 (SL a 10 ticks de VWAP)
        self.assertEqual(ticks_sl, 16)         # (20002.50 - 19998.50) / 0.25 = 16 ticks
        self.assertEqual(ticks_tp, -96)        # -96 ticks por 60% de banda inferior
        self.assertEqual(precio_tp, 19974.50)

    def test_tp_techo_60_porciento_banda(self):
        """El TP debe situarse exactamente al 60% de la distancia entre la línea VWAP y la banda que sigue."""
        # Banda a solo 10 puntos del VWAP: 60% de 10 puntos = 6.0 puntos = 24 ticks
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20000.0, 19998.0, 20010.0, 19990.0, "NQ", 20000.0
        )
        self.assertEqual(ticks_tp, 24) # 60% de 10 puntos = 24 ticks
        self.assertEqual(precio_tp, 20006.0)

    def test_tp_piso_minimo_5_ticks(self):
        """Si la banda está muy pegada (ej. 2 puntos), el TP debe respetar el piso de 5 ticks."""
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20000.0, 19998.0, 20002.0, 19998.0, "NQ", 20000.0
        )
        self.assertEqual(ticks_tp, 5) # Piso mínimo de 5 ticks

    def test_volumen_insuficiente_ignora_setup(self):
        """Si el volumen es solo +5% (1050 vs 1000), debe abortar por falta de esfuerzo."""
        df_1m = self._crear_df_1m(
            vol_cerrada=1050,  # Menos de 1.10x (1100 requerido)
            open_p=20002.0,
            high_p=20003.0,
            low_p=19999.50,
            close_p=20001.50
        )
        setup = self.engine.verificar_absorcion_vwap_1m(self.df_5m, "NQ", df_1m_override=df_1m)
        self.assertIsNone(setup, "No debe disparar si el volumen no supera el +10%")

    def test_ruptura_sin_rechazo_ignora_setup(self):
        """Si hubo mucho volumen pero el cuerpo atravesó el VWAP hacia abajo (ruptura), no opera."""
        df_1m = self._crear_df_1m(
            vol_cerrada=1500,  # Esfuerzo alto (+50%)
            open_p=20002.0,
            high_p=20003.0,
            low_p=19996.00,
            close_p=19997.00   # Cerró perforando el VWAP hacia abajo
        )
        setup = self.engine.verificar_absorcion_vwap_1m(self.df_5m, "NQ", df_1m_override=df_1m)
        self.assertIsNone(setup)

    def test_filtro_tendencia_5m_ema50_bloquea_contra_tendencia(self):
        """Verifica que el Filtro de Tendencia Superior 5m bloquee compras si el precio 5m está bajo la EMA50."""
        # Configurar 5m con EMA50 por encima del precio actual (tendencia bajista)
        df_5m_bajista = self.df_5m.copy()
        df_5m_bajista['EMA50'] = 20050.0 # Precio en 20002.0 < EMA50 20050.0 (Bajista)
        
        df_1m = self._crear_df_1m(
            vol_cerrada=1150,
            open_p=20002.0,
            high_p=20003.0,
            low_p=19999.50,
            close_p=20001.50
        )
        # Intento de LONG en mercado bajista debe ser bloqueado por el filtro de tendencia
        setup_bloqueado = self.engine.verificar_absorcion_vwap_1m(df_5m_bajista, "NQ", df_1m_override=df_1m)
        self.assertIsNone(setup_bloqueado, "El Filtro de Tendencia 5m debe bloquear compras contra-tendencia")

        # Con EMA50 por debajo del precio (tendencia alcista), el LONG debe ser aprobado
        df_5m_alcista = self.df_5m.copy()
        df_5m_alcista['EMA50'] = 19950.0 # Precio en 20002.0 > EMA50 19950.0 (Alcista)
        setup_aprobado = self.engine.verificar_absorcion_vwap_1m(df_5m_alcista, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup_aprobado, "El LONG debe ser aprobado cuando 5m está por encima de EMA50")
        self.assertEqual(setup_aprobado["direccion"], "LONG")

    def test_escalera_3_hitos_progresivos(self):
        """Verifica la Escalera de 3 Hitos: H1 (40% -> BE+1t), H2 (75% -> +35% ganancia), H3 (90% -> +60% ganancia)."""
        self.engine.symbol_states["NQ"]["has_open_position"] = True
        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_NQ_TEST", "size": 1}]
        self.mock_client.search_open_orders.return_value = [{"id": 9999, "contractId": "CON_NQ_TEST", "type": 4, "size": 1}]
        self.mock_client.modify_order.return_value = {"success": True}

        entry_p = 20000.0
        tp_p = 20020.0 # 20 puntos = 80 ticks
        be_p = 20000.25 # Entrada + 1 tick

        self.engine.symbol_states["NQ"]["active_trade"] = {
            "symbol": "NQ",
            "contract_id": "CON_NQ_TEST",
            "direction": "LONG",
            "entry_price": entry_p,
            "current_price": entry_p,
            "initial_sl_price": 19995.0,
            "current_sl_price": 19995.0,
            "tp_price": tp_p,
            "initial_sl_ticks": -20,
            "tp_ticks": 80,
            "rvol": 1.80,
            "order_id": 8888,
            "sl_stage": 0,
            "entry_timestamp": 100.0,
            "last_check_time": 0.0
        }

        # 1. Precio alcanza el 52.5% (20010.50) -> Dispara Hito 1 (SL a BE + 1 tick = 20000.25)
        df_h1 = pd.DataFrame([{'Close': 20010.50}])
        self.engine.monitorear_posicion_abierta("NQ", df_h1)
        trade = self.engine.symbol_states["NQ"]["active_trade"]
        self.assertEqual(trade["sl_stage"], 1, "Debe estar en Hito 1")
        self.assertEqual(trade["current_sl_price"], be_p, "El SL debe ser Entrada + 1 tick (20000.25)")

        # 2. Precio alcanza el 76% (20015.25) -> Dispara Hito 2 (SL asegura +35% de ganancia = 20007.00)
        trade["last_check_time"] = 0.0 # reset check throttle
        df_h2 = pd.DataFrame([{'Close': 20015.25}])
        self.engine.monitorear_posicion_abierta("NQ", df_h2)
        self.assertEqual(trade["sl_stage"], 2, "Debe estar en Hito 2")
        self.assertEqual(trade["current_sl_price"], 20007.00, "El SL debe asegurar +35% (20007.00)")

        # 3. Precio alcanza el 91% (20018.25) -> Dispara Hito 3 (SL asegura +60% = 20012.00) y expande TP (+40% = 20028.00)
        trade["last_check_time"] = 0.0
        df_h3 = pd.DataFrame([{'Close': 20018.25}])
        self.engine.monitorear_posicion_abierta("NQ", df_h3)
        self.assertEqual(trade["sl_stage"], 3, "Debe estar en Hito 3")
        self.assertEqual(trade["current_sl_price"], 20012.00, "El SL debe asegurar +60% (20012.00)")
        self.assertEqual(trade["tp_extensions"], 1, "El TP debe haberse expandido 1 vez")
        self.assertEqual(trade["tp_price"], 20028.00, "El nuevo TP expandido debe ser 20028.00 (+8 pts)")

    def test_independencia_entre_simbolos(self):
        """Verifica que un trade abierto en NQ no interfiere ni contamina el estado de ES."""
        self.engine.symbol_states["NQ"]["active_trade"] = {"symbol": "NQ", "direction": "LONG"}
        self.engine.symbol_states["NQ"]["has_open_position"] = True

        self.assertIsNone(self.engine.symbol_states["ES"]["active_trade"])
        self.assertFalse(self.engine.symbol_states["ES"]["has_open_position"])

    def test_auto_adopcion_posicion_manual_mcl(self):
        """
        Verifica el caso exacto reportado por el usuario:
        Una operación Short en MCL abierta en 98.12 con SL en 98.32 y TP en 97.50.
        El bot la detecta y adopta automáticamente, y al llegar el precio a 97.77 (>50% de TP),
        dispara el Hito 1 moviendo el SL a Break-Even + 1 tick (98.11) enviando stopPrice a TopstepX.
        """
        contract_mcl = "CON.F.US.MCLE.Z26"
        self.mock_client.get_open_positions.return_value = [{
            "id": 11111,
            "accountId": 12345,
            "contractId": contract_mcl,
            "type": 2, # Short
            "size": -1,
            "averagePrice": 98.12
        }]
        self.mock_client.search_open_orders.return_value = [
            {"id": 3663652569, "contractId": contract_mcl, "type": 4, "size": 1, "stopPrice": 98.32},
            {"id": 3663652570, "contractId": contract_mcl, "type": 1, "size": 1, "limitPrice": 97.50}
        ]
        self.mock_client.modify_order.return_value = {"success": True, "errorCode": 0}

        # Inicialmente no hay active_trade en memoria
        self.engine.symbol_states["MCL"]["active_trade"] = None
        self.engine.symbol_states["MCL"]["has_open_position"] = True

        # Precio de mercado llega a 97.77 (98.12 - 97.77 = 0.35 pts de ganancia; TP total es 98.12 - 97.50 = 0.62 pts; 56.5% de avance)
        df_mcl = pd.DataFrame([{'Close': 97.77}])
        self.engine.monitorear_posicion_abierta("MCL", df_mcl)

        trade = self.engine.symbol_states["MCL"]["active_trade"]
        self.assertIsNotNone(trade, "La posición en MCL debe haber sido adoptada automáticamente")
        self.assertEqual(trade["direction"], "SHORT")
        self.assertEqual(trade["entry_price"], 98.12)
        self.assertEqual(trade["tp_price"], 97.50)
        self.assertEqual(trade["sl_stage"], 1, "Debe alcanzar Hito 1 (avance 56.5% >= 40%)")
        self.assertEqual(trade["current_sl_price"], 98.11, "SL en SHORT debe ser Entrada - 1 tick = 98.11")

        # Verificar que modify_order fue llamado con stop_price=98.11
        self.mock_client.modify_order.assert_called_with(
            account_id=12345,
            order_id=3663652569,
            stop_price=98.11,
            size=1
        )

    def test_modify_order_payload_structure(self):
        """Verifica que TopstepClient.modify_order serialice stopPrice y limitPrice correctamente."""
        from unittest.mock import patch
        client = TopstepClient()
        client.token = "mock_token"
        client.token_expiry = 9999999999.0
        
        with patch("requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"success": True, "errorCode": 0}
            
            client.modify_order(account_id=12345, order_id=9876, stop_price=20005.25, size=1)
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args[1]
            json_payload = call_kwargs["json"]
            self.assertEqual(json_payload["accountId"], 12345)
            self.assertEqual(json_payload["orderId"], 9876)
            self.assertEqual(json_payload["stopPrice"], 20005.25)
            self.assertNotIn("price", json_payload)

    def test_condicion_vela_5m_previa_limpia(self):
        """
        Verifica la condición extra:
        La vela de 5 minutos anterior debe haber estado completa del lado del rebote sin tocar ni pasar el VWAP.
        """
        df_1m = self._crear_df_1m(vol_cerrada=1200, open_p=20002.0, high_p=20003.0, low_p=19999.50, close_p=20001.50)
        
        # Caso 1: Vela previa de 5m limpia arriba de VWAP (Low: 20002.0 > VWAP 20000.0) -> ACEPTA
        df_5m_valido = pd.DataFrame([
            {'Timestamp': self.now - timedelta(minutes=5), 'Open': 20005.0, 'High': 20010.0, 'Low': 20002.0, 'Close': 20006.0, 'Volume': 4000, 'VWAP': 20000.0, 'Upper_Band': 20040.0, 'Lower_Band': 19960.0},
            {'Timestamp': self.now, 'Open': 20006.0, 'High': 20008.0, 'Low': 19999.5, 'Close': 20002.0, 'Volume': 5000, 'VWAP': 20000.0, 'Upper_Band': 20040.0, 'Lower_Band': 19960.0}
        ])
        setup_valido = self.engine.verificar_absorcion_vwap_1m(df_5m_valido, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup_valido, "Debe aceptar setup si la vela previa estuvo completamente arriba de VWAP")

        # Caso 2: Vela previa de 5m tocó o cruzó el VWAP (Low: 19998.0 <= VWAP 20000.0) -> RECHAZA
        df_5m_invalido = pd.DataFrame([
            {'Timestamp': self.now - timedelta(minutes=5), 'Open': 20005.0, 'High': 20007.0, 'Low': 19998.0, 'Close': 20001.0, 'Volume': 4000, 'VWAP': 20000.0, 'Upper_Band': 20040.0, 'Lower_Band': 19960.0},
            {'Timestamp': self.now, 'Open': 20001.0, 'High': 20005.0, 'Low': 19999.5, 'Close': 20002.0, 'Volume': 5000, 'VWAP': 20000.0, 'Upper_Band': 20040.0, 'Lower_Band': 19960.0}
        ])
        setup_invalido = self.engine.verificar_absorcion_vwap_1m(df_5m_invalido, "NQ", df_1m_override=df_1m)
        self.assertIsNone(setup_invalido, "Debe rechazar si la vela previa de 5m tocó o cruzó el VWAP")

    def test_fast_tracker_mecha_viva(self):
        """
        Verifica que el Fast Tracker reconozca el hito si la mecha viva (Low/High) tocó la meta,
        incluso si el precio de cierre en ese instante rebotó.
        """
        self.engine.symbol_states["NQ"]["has_open_position"] = True
        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_NQ_TEST", "size": 1}]
        self.mock_client.search_open_orders.return_value = [{"id": 7777, "contractId": "CON_NQ_TEST", "type": 4, "size": 1}]
        self.mock_client.modify_order.return_value = {"success": True}

        entry_p = 20000.0
        tp_p = 20020.0
        
        self.engine.symbol_states["NQ"]["active_trade"] = {
            "symbol": "NQ",
            "contract_id": "CON_NQ_TEST",
            "direction": "LONG",
            "entry_price": entry_p,
            "current_price": entry_p,
            "initial_sl_price": 19997.50,
            "current_sl_price": 19997.50,
            "tp_price": tp_p,
            "tp_ticks": 80,
            "sl_stage": 0,
            "last_check_time": 0.0
        }

        # La vela viva tiene Close en 20005.0 (avance 25% < 50%), pero su High llegó a 20011.0 (avance 55% >= 50%)
        df_live = pd.DataFrame([{'Open': 20003.0, 'High': 20011.0, 'Low': 20002.0, 'Close': 20005.0}])
        self.engine.monitorear_posicion_abierta("NQ", df_live)

        trade = self.engine.symbol_states["NQ"]["active_trade"]
        self.assertEqual(trade["sl_stage"], 1, "Debe alcanzar Hito 1 gracias al High de la mecha viva")
        self.assertEqual(trade["current_sl_price"], 20000.25, "SL debe moverse a Break-Even + 1 tick")
        self.assertGreaterEqual(trade["max_pct_avance"], 50.0)

    def test_csv_hitos_y_max_pct_tp(self):
        """Verifica que el registro de CSV y el historial incluyan Hito_Alcanzado y Max_Pct_TP."""
        # 1. Test format_milestone
        self.assertEqual(self.engine.format_milestone(0), "Inicial")
        self.assertEqual(self.engine.format_milestone(1), "Hito 1 (50% BE)")
        self.assertEqual(self.engine.format_milestone(2), "Hito 2 (75% +35%)")
        self.assertEqual(self.engine.format_milestone(3), "Hito 3 (90% +60%)")
        self.assertEqual(self.engine.format_milestone(4), "Hito 4 (125% TP)")

        # 2. Test registrar_evento_be_en_csv
        trade_mock = {
            "direction": "LONG",
            "entry_price": 5000.0,
            "current_price": 5010.0,
            "sl_stage": 2,
            "tp_ticks": "Dinámico",
            "max_pct_avance": 76.5,
            "order_id": "TEST_BE_ORDER"
        }
        self.engine.registrar_evento_be_en_csv("ES", trade_mock, 5007.0, 76.5, "Hito 2 alcanzado")
        
        # 3. Test load_operations_history
        ops = self.engine.load_operations_history()
        self.assertTrue(len(ops) > 0)
        latest_op = next((op for op in reversed(ops) if op.get("Order_ID") == "TEST_BE_ORDER"), None)
        self.assertIsNotNone(latest_op)
        self.assertEqual(latest_op["Hito_Alcanzado"], "Hito 2 (75% +35%)")
        self.assertEqual(latest_op["Max_Pct_TP"], "76.5%")

if __name__ == '__main__':
    unittest.main()
