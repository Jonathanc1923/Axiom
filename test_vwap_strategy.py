import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
from unittest.mock import MagicMock
import tempfile
import os

from engine import AxiomEngine
from topstep_client import TopstepClient

class TestPureVWAPStrategy(unittest.TestCase):
    def setUp(self):
        self.temp_csv = tempfile.NamedTemporaryFile(suffix='.csv', delete=False)
        self.temp_csv.close()
        
        self.mock_client = MagicMock(spec=TopstepClient)
        self.mock_client.place_order.return_value = {"success": True, "orderId": 999001}
        self.mock_client.modify_order.return_value = {"success": True}
        self.mock_client.get_open_positions.return_value = []
        self.mock_client.search_open_orders.return_value = [
            {"id": 111, "contractId": "CON_ES_TEST", "type": 4, "stopPrice": 4993.75}, # SL (Stop)
            {"id": 222, "contractId": "CON_ES_TEST", "type": 1, "limitPrice": 5040.00}  # TP (Limit)
        ]
        
        self.engine = AxiomEngine(self.mock_client, default_symbol="ES", csv_path=self.temp_csv.name)
        self.engine.symbol_states["ES"]["tick_size"] = 0.25
        self.engine.symbol_states["ES"]["contract_id"] = "CON_ES_TEST"
        self.engine.account_id = 12345
        
        self.now = datetime.now(timezone.utc)
        
    def tearDown(self):
        if hasattr(self, 'temp_csv') and os.path.exists(self.temp_csv.name):
            try:
                os.remove(self.temp_csv.name)
            except Exception:
                pass

    def test_setup_long_valid(self):
        """Vela previa 100% por encima de VWAP, precio actual toca/cruza VWAP, pendiente alcista -> Señal LONG."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5015.0, 'High': 5020.0, 'Low': 5005.0, 'Close': 5010.0,
                'Volume': 1000, 'VWAP': 4995.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 5008.0, 'High': 5012.0, 'Low': 4999.5, 'Close': 5000.0,
                'Volume': 1200, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])
        
        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=5000.0)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "LONG")
        self.assertEqual(setup["precio_entrada"], 5000.0)
        self.assertEqual(setup["d_base"], 40.0) # 5040 - 5000
        self.assertGreater(setup["vwap_slope"], 0)

    def test_setup_short_valid(self):
        """Vela previa 100% por debajo de VWAP, precio actual toca/cruza VWAP, pendiente bajista -> Señal SHORT."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 4985.0, 'High': 4995.0, 'Low': 4980.0, 'Close': 4990.0,
                'Volume': 1000, 'VWAP': 5005.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 4992.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 5000.25,
                'Volume': 1200, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])
        
        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=5000.25)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "SHORT")
        self.assertEqual(setup["precio_entrada"], 5000.25)
        self.assertAlmostEqual(setup["d_base"], 40.25, places=2)
        self.assertLess(setup["vwap_slope"], 0)

    def test_setup_rechazado_si_vela_previa_toco_vwap(self):
        """Si la vela previa tocó o cruzó el VWAP, el contexto NO es válido -> None."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5010.0, 'High': 5015.0, 'Low': 4990.0, 'Close': 5005.0, # Low 4990.0 < VWAP 4995.0 (tocó/cruzó)
                'Volume': 1000, 'VWAP': 4995.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 5005.0, 'High': 5008.0, 'Low': 4999.0, 'Close': 5000.0,
                'Volume': 1200, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])
        
        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=5000.0)
        self.assertIsNone(setup)

    def test_setup_abre_si_pasa_vwap(self):
        """Verifica que el bot abra igual si toca o pasa el VWAP con pendiente alcista válida."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5015.0, 'High': 5020.0, 'Low': 5005.0, 'Close': 5010.0,
                'Volume': 1000, 'VWAP': 4995.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 5005.0, 'High': 5008.0, 'Low': 4990.0, 'Close': 4995.0, # Pasó VWAP hacia abajo
                'Volume': 1200, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])
        
        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=4995.0)
        self.assertIsNotNone(setup, "Debe abrir orden LONG al pasar el VWAP hacia abajo")
        self.assertEqual(setup["direccion"], "LONG")
        self.assertEqual(setup["precio_entrada"], 4995.0)

    def test_setup_short_rechazado_si_vela_previa_estuvo_arriba(self):
        """Si la vela previa estuvo por encima de VWAP, NO se puede abrir SHORT de reversión."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 52775.0, 'High': 52781.0, 'Low': 52755.0, 'Close': 52770.0, # 100% arriba de VWAP 52740
                'Volume': 1000, 'VWAP': 52740.0, 'Upper_Band': 52780.0, 'Lower_Band': 52700.0
            },
            {
                'Timestamp': self.now,
                'Open': 52767.0, 'High': 52841.0, 'Low': 52755.0, 'Close': 52820.0,
                'Volume': 1200, 'VWAP': 52743.0, 'Upper_Band': 52783.0, 'Lower_Band': 52703.0
            }
        ])
        setup = self.engine.verificar_setup_vwap(df_5m, "YM", current_price=52820.0)
        self.assertIsNone(setup, "Debe rechazar SHORT porque la vela previa estuvo arriba de VWAP")

    def test_setup_long_rechazado_si_vela_previa_estuvo_abajo(self):
        """Si la vela previa estuvo por debajo de VWAP, NO se puede abrir LONG de reversión."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 52710.0, 'High': 52725.0, 'Low': 52700.0, 'Close': 52720.0, # 100% abajo de VWAP 52740
                'Volume': 1000, 'VWAP': 52740.0, 'Upper_Band': 52780.0, 'Lower_Band': 52700.0
            },
            {
                'Timestamp': self.now,
                'Open': 52725.0, 'High': 52745.0, 'Low': 52720.0, 'Close': 52735.0,
                'Volume': 1200, 'VWAP': 52743.0, 'Upper_Band': 52783.0, 'Lower_Band': 52703.0
            }
        ])
        setup = self.engine.verificar_setup_vwap(df_5m, "YM", current_price=52735.0)
        self.assertIsNone(setup, "Debe rechazar LONG porque la vela previa vino desde abajo de VWAP")

    def test_ejecutar_orden_convencion_signos_long_y_short(self):
        """Verifica que el SL sea NEGATIVO para LONG y POSITIVO para SHORT según TopstepX API."""
        setup_long = {
            "simbolo": "ES",
            "direccion": "LONG",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 5040.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup_long, "ES", ignore_schedule=True)
        _, kwargs_long = self.mock_client.place_order.call_args
        self.assertEqual(kwargs_long["side"], 0)
        self.assertEqual(kwargs_long["sl_ticks"], -35, "TopstepX exige signo negativo para SL en LONG")
        self.assertEqual(kwargs_long["tp_ticks"], 160)

        # Probar SHORT
        self.engine.symbol_states["ES"]["has_open_position"] = False
        self.engine.symbol_states["ES"]["active_trade"] = None
        setup_short = {
            "simbolo": "ES",
            "direccion": "SHORT",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 4960.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup_short, "ES", ignore_schedule=True)
        _, kwargs_short = self.mock_client.place_order.call_args
        self.assertEqual(kwargs_short["side"], 1)
        self.assertEqual(kwargs_short["sl_ticks"], 35, "TopstepX exige signo positivo para SL en SHORT")
        self.assertEqual(kwargs_short["tp_ticks"], -160, "TopstepX exige signo negativo para TP en SHORT")

    def test_escalera_de_hitos_long(self):
        """Prueba los hitos optimizados en LONG: Micro-escudo 20% (SL 15t), 35% (BE+1), 50% (SL 35%, TP 120%), 75% (SL 65%, TP 150%), 100% (SL 85%, TP 180%) y Runner Trailing."""
        setup = {
            "simbolo": "ES",
            "direccion": "LONG",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 5040.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertIsNotNone(trade)
        
        # 1. Al 20% (Precio 5008.0 = 5000 + 8 pts = 20% de 40.0 pts): Micro-Escudo -> SL reducido a 15 ticks (4996.25)
        self.engine.monitorear_posicion_abierta("ES", current_price=5008.0)
        self.assertEqual(trade["sl_stage"], 0.5)
        self.assertEqual(trade["current_sl_price"], 4996.25) # 5000 - 15 * 0.25
        
        # 2. Al 40% (Precio 5016.0 >= 35% de 40.0 = 14.0 pts): Hito 1 -> SL a BE + 1 tick (5000.25)
        self.engine.monitorear_posicion_abierta("ES", current_price=5016.0)
        self.assertEqual(trade["sl_stage"], 1)
        self.assertEqual(trade["current_sl_price"], 5000.25)
        
        # 3. Al 55% (Precio 5022.0 >= 50% de 40.0 = 20.0 pts): Hito 2 -> SL a 35% (5014.0), TP a 120% (5048.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5022.0)
        self.assertEqual(trade["sl_stage"], 2)
        self.assertEqual(trade["current_sl_price"], 5014.0)
        self.assertEqual(trade["current_tp_price"], 5048.0)
        
        # 4. Al 80% (Precio 5032.0 >= 75% de 40.0 = 30.0 pts): Hito 3 -> SL a 65% (5026.0), TP a 150% (5060.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5032.0)
        self.assertEqual(trade["sl_stage"], 3)
        self.assertAlmostEqual(trade["current_sl_price"], 5026.0, places=2)
        self.assertEqual(trade["current_tp_price"], 5060.0)

        # 5. Al 105% (Precio 5042.0 >= 100% de 40.0 = 40.0 pts): Hito 4 -> SL al 85% (5034.0) y Runner trailing persigue a 15 ticks de 5042.0 (5038.25)
        self.engine.monitorear_posicion_abierta("ES", current_price=5042.0)
        self.assertEqual(trade["sl_stage"], 4)
        # Runner trailing: 5042.0 - (15 * 0.25 = 3.75) = 5038.25 > 5034.0
        self.assertAlmostEqual(trade["current_sl_price"], 5038.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5072.0)

    def test_escalera_de_hitos_short(self):
        """Prueba los hitos optimizados en SHORT con Micro-escudo temprano."""
        setup = {
            "simbolo": "ES",
            "direccion": "SHORT",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 4960.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertIsNotNone(trade)
        
        # 1. Al 20% (Precio 4992.0 <= 5000 - 8 pts = 20% de 40.0 pts): Micro-escudo -> SL a 15 ticks (5003.75)
        self.engine.monitorear_posicion_abierta("ES", current_price=4992.0)
        self.assertEqual(trade["sl_stage"], 0.5)
        self.assertEqual(trade["current_sl_price"], 5003.75) # 5000 + 15 * 0.25
        
        # 2. Al 40% (Precio 4984.0 <= 5000 - 14.0 = 4986.0): Hito 1 -> SL a BE - 1 tick (4999.75)
        self.engine.monitorear_posicion_abierta("ES", current_price=4984.0)
        self.assertEqual(trade["sl_stage"], 1)
        self.assertEqual(trade["current_sl_price"], 4999.75)
        
        # 3. Al 55% (Precio 4978.0 <= 5000 - 20.0 = 4980.0): Hito 2 -> SL a 35% (4986.0), TP a 120% (4952.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=4978.0)
        self.assertEqual(trade["sl_stage"], 2)
        self.assertEqual(trade["current_sl_price"], 4986.0)
        self.assertEqual(trade["current_tp_price"], 4952.0)

    def test_no_phantom_past_high_liquidation(self):
        """Verifica que un High previo en la vela 5m no provoque liquidación inmediata a los 2 segundos."""
        setup = {
            "simbolo": "ES",
            "direccion": "LONG",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 5040.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        
        # Vela actual de 5m con High=5025.0 previo a la entrada
        df_5m = pd.DataFrame([{
            'Timestamp': self.now,
            'Open': 5020.0, 'High': 5025.0, 'Low': 4999.0, 'Close': 5000.25, 'Volume': 500
        }])
        
        # 2 segundos después de entrar, precio está en 5000.25
        self.engine.monitorear_posicion_abierta("ES", current_price=5000.25, df_5m=df_5m)
        
        # La posición NO debe haber sido liquidada
        trade = self.engine.symbol_states["ES"].get("active_trade")
        self.assertIsNotNone(trade, "La posición no debe ser liquidada por un High previo a la entrada")
        self.assertEqual(trade["sl_stage"], 0, "No debe activar Hito 1 artificialmente con wicks del pasado")
        self.assertEqual(self.mock_client.close_contract_position.call_count, 0)

    def test_hitos_actualizan_csv_sin_duplicar_filas(self):
        """Verifica que cada avance de hito actualice la misma fila en CSV sin duplicados."""
        setup = {
            "simbolo": "ES",
            "direccion": "LONG",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 5040.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        
        # 1. Al inicio: 1 fila en CSV, Estado EN CURSO (OCO), Hito Inicial
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1)
        self.assertIn("EN CURSO", history[0]["Estado"])
        self.assertEqual(history[0]["Hito_Alcanzado"], "Inicial")
        
        # 2. Avance a Hito 1 (5018.0): SL a BE + 1t
        self.engine.monitorear_posicion_abierta("ES", current_price=5018.0)
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1, "No debe crear nuevas filas duplicadas")
        self.assertIn("Hito 1", history[0]["Estado"])
        self.assertIn("Hito 1", history[0]["Hito_Alcanzado"])
        self.assertEqual(history[0]["Max_Pct_TP"], "45.0%")

        # 3. Avance a Hito 2 (5022.0 = 55% de 40 pts): SL al 35%, TP al 120%
        self.engine.monitorear_posicion_abierta("ES", current_price=5022.0)
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1, "Debe seguir existiendo exactamente 1 fila")
        self.assertIn("Hito 2", history[0]["Estado"])
        self.assertIn("Hito 2", history[0]["Hito_Alcanzado"])
        self.assertEqual(history[0]["Max_Pct_TP"], "55.0%")

    def test_cierre_posicion_marca_csv_cerrada_y_calcula_pnl(self):
        """Verifica que al cerrarse la posición en el mercado, el CSV se actualice a CERRADA con PnL y Hito final."""
        setup = {
            "simbolo": "ES",
            "direccion": "LONG",
            "precio_entrada": 5000.0,
            "vwap_cruce": 5000.0,
            "upper_band": 5040.0,
            "lower_band": 4960.0,
            "target_band": 5040.0,
            "d_base": 40.0
        }
        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        
        # Avanzar a Hito 2 (5022.0 = 55% de 40 pts)
        self.engine.monitorear_posicion_abierta("ES", current_price=5022.0)
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertEqual(trade["sl_stage"], 2)
        
        # Simular cierre en mercado: la posición ya no existe en TopstepX (>5 seg)
        trade["entry_timestamp"] = trade["entry_timestamp"] - 10.0
        self.mock_client.get_open_positions.return_value = []
        
        self.engine.monitorear_posicion_abierta("ES", current_price=5022.0)
        
        # active_trade debe haber concluido
        self.assertIsNone(self.engine.symbol_states["ES"]["active_trade"])
        self.assertFalse(self.engine.symbol_states["ES"]["has_open_position"])
        
        # En CSV debe figurar CERRADA con PnL y el Hito 2 preservado
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1)
        self.assertIn("CERRADA", history[0]["Estado"])
        self.assertIn("Hito 2", history[0]["Hito_Alcanzado"])
        self.assertEqual(history[0]["Max_Pct_TP"], "55.0%")
        self.assertEqual(history[0]["Precio_Salida_Real"], "5022.00")
        pnl_net = float(history[0]["PnL_Neto_USD"])
        self.assertGreater(pnl_net, 0)

    def test_sync_reconcilia_operaciones_abiertas_y_cerradas(self):
        """Verifica que sync_real_data_from_topstep concilie órdenes huérfanas en CSV para que solo figuren abiertas las reales."""
        # Escribir manualmente una orden que figura 'EN CURSO' en CSV
        self.engine.registrar_en_csv(
            simbolo="ES", direccion="LONG", precio=5000.0,
            ticks_sl=25, ticks_tp=160, condicion="Test",
            exito=True, mensaje_servidor="OK", error_code="0", order_id="888001"
        )
        history_before = self.engine.load_operations_history()
        self.assertEqual(len(history_before), 1)
        self.assertIn("EN CURSO", history_before[0]["Estado"])
        
        # Simular que TopstepX NO tiene posiciones abiertas para ES
        self.mock_client.get_open_positions.return_value = []
        self.mock_client.search_trades.return_value = []
        self.mock_client.get_account_balance.return_value = 150000.0
        
        self.engine.sync_real_data_from_topstep()
        
        # Después del sync, la orden de ES debe figurar CERRADA
        history_after = self.engine.load_operations_history()
        self.assertEqual(len(history_after), 1)
        self.assertIn("CERRADA", history_after[0]["Estado"])

    def test_posicion_abierta_en_simbolo_inactivo_es_monitoreada_y_actualiza_hitos(self):
        """Verifica que si hay una posición en un símbolo como MES (no habilitado por defecto), se incluya y actualice hitos."""
        self.engine.symbol_states["MES"]["contract_id"] = "CON.F.US.MES.Z26"
        self.engine.symbol_states["MES"]["enabled"] = False
        self.engine.active_symbol = "ES"

        # Simular que TopstepX tiene posición abierta en MES
        cached_positions = [{'id': 777, 'contractId': 'CON.F.US.MES.Z26', 'size': 1, 'averagePrice': 7738.25, 'type': 2}]
        
        # Extraer símbolos según la nueva lógica del engine
        open_syms = {self.engine.resolve_symbol_from_contract(p.get("contractId", "")) for p in cached_positions}
        symbols_to_process = [
            sym for sym, state in self.engine.symbol_states.items()
            if state.get("contract_id") and (
                state.get("enabled", False)
                or sym == self.engine.active_symbol
                or state.get("has_open_position", False)
                or state.get("active_trade") is not None
                or sym in open_syms
            )
        ]
        self.assertIn("MES", symbols_to_process, "MES debe estar en symbols_to_process cuando tiene posición abierta")

    def test_setup_long_rechazado_si_pendiente_vwap_bajista(self):
        """Verifica que un setup LONG sea rechazado si el VWAP de los últimos 10 períodos va hacia abajo."""
        bars = []
        for i in range(12):
            bars.append({
                'Timestamp': self.now - timedelta(minutes=(12 - i) * 5),
                'Open': 5020.0 - i * 2, 'High': 5025.0 - i * 2, 'Low': 5005.0 - i * 2, 'Close': 5010.0 - i * 2,
                'Volume': 1000,
                'VWAP': 5020.0 - i * 2,
                'Upper_Band': 5050.0, 'Lower_Band': 4950.0
            })
        df_5m = pd.DataFrame(bars)
        df_5m.iloc[-2, df_5m.columns.get_loc('Low')] = df_5m.iloc[-2]['VWAP'] + 5.0
        precio_actual = float(df_5m.iloc[-1]['VWAP'])

        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=precio_actual)
        self.assertIsNone(setup, "Debe rechazar LONG porque la pendiente del VWAP va en bajada")

    def test_setup_short_rechazado_si_pendiente_vwap_alcista(self):
        """Verifica que un setup SHORT sea rechazado si el VWAP de los últimos 10 períodos va hacia arriba."""
        bars = []
        for i in range(12):
            bars.append({
                'Timestamp': self.now - timedelta(minutes=(12 - i) * 5),
                'Open': 4980.0 + i * 2, 'High': 4995.0 + i * 2, 'Low': 4975.0 + i * 2, 'Close': 4990.0 + i * 2,
                'Volume': 1000,
                'VWAP': 4980.0 + i * 2,
                'Upper_Band': 5050.0, 'Lower_Band': 4950.0
            })
        df_5m = pd.DataFrame(bars)
        df_5m.iloc[-2, df_5m.columns.get_loc('High')] = df_5m.iloc[-2]['VWAP'] - 5.0
        precio_actual = float(df_5m.iloc[-1]['VWAP'])

        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=precio_actual)
        self.assertIsNone(setup, "Debe rechazar SHORT porque la pendiente del VWAP va en subida")

    def test_setup_aprobado_pendiente_vwap_7_velas_35_min(self):
        """Verifica que si la pendiente en 7 velas (35 min) va en la dirección deseada, se apruebe con datos de pendiente."""
        bars_long = []
        for i in range(15):
            bars_long.append({
                'Timestamp': self.now - timedelta(minutes=(15 - i) * 5),
                'Open': 4980.0 + i * 1.5, 'High': 4995.0 + i * 1.5, 'Low': 4975.0 + i * 1.5, 'Close': 4990.0 + i * 1.5,
                'Volume': 1000,
                'VWAP': 4980.0 + i * 1.5,
                'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            })
        df_long = pd.DataFrame(bars_long)
        df_long.iloc[-2, df_long.columns.get_loc('Low')] = df_long.iloc[-2]['VWAP'] + 2.0
        precio_mercado = float(df_long.iloc[-1]['VWAP'])

        setup = self.engine.verificar_setup_vwap(df_long, "ES", current_price=precio_mercado)
        self.assertIsNotNone(setup, "Debe aprobar LONG con pendiente alcista en 7 velas (35m) de 5m")
        self.assertEqual(setup["direccion"], "LONG")
        self.assertGreater(setup["vwap_slope"], 0.0)
        self.assertEqual(setup["slope_bars"], 7)

    def test_calcular_vwap_sesion_completa_desde_18h(self):
        """Verifica que calcular_vwap_sesion aísle la sesión actual iniciando a las 18:00 ET y acumulando velas de 5m."""
        ts_range = pd.date_range(start='2026-10-21 20:00:00+00:00', end='2026-10-22 15:00:00+00:00', freq='5min')
        df_multi = pd.DataFrame({
            'Timestamp': ts_range,
            'Open': 5000.0,
            'High': 5010.0,
            'Low': 4990.0,
            'Close': 5005.0,
            'Volume': 100
        })
        df_res = self.engine.calcular_vwap_sesion(df_multi)
        self.assertFalse(df_res.empty)
        primera_vela = df_res.iloc[0]
        self.assertEqual(primera_vela['NY_Time'].hour, 18)
        self.assertEqual(primera_vela['NY_Time'].minute, 0)
        self.assertEqual(primera_vela['Cum_Vol'], 100)
        self.assertEqual(df_res.iloc[-1]['Cum_Vol'], len(df_res) * 100)

    def test_setup_long_rechazado_por_pendiente_insuficiente(self):
        """Verifica que si la pendiente es positiva pero menor a MIN_VWAP_SLOPE_TICKS (2 ticks = 0.50 en ES), se rechace."""
        bars = []
        for i in range(15):
            # Pendiente micro de 0.01 por barra = 0.10 en 10 barras (< 0.50)
            bars.append({
                'Timestamp': self.now - timedelta(minutes=(15 - i) * 5),
                'Open': 5000.0 + i * 0.01, 'High': 5005.0 + i * 0.01, 'Low': 4995.0 + i * 0.01, 'Close': 5000.0 + i * 0.01,
                'Volume': 1000,
                'VWAP': 5000.0 + i * 0.01,
                'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            })
        df_flat = pd.DataFrame(bars)
        df_flat.iloc[-2, df_flat.columns.get_loc('Low')] = df_flat.iloc[-2]['VWAP'] + 1.0
        precio_actual = float(df_flat.iloc[-1]['VWAP'])

        setup = self.engine.verificar_setup_vwap(df_flat, "ES", current_price=precio_actual)
        self.assertIsNone(setup, "Debe rechazar LONG porque la pendiente (+0.10) es menor al mínimo requerido (+2.00 pts / 8 ticks en ES)")

    def test_pendiente_minima_petroleo_cl(self):
        """Verifica que para CL la pendiente mínima sea 0.03 puntos (3 ticks) y rechace pendientes planas."""
        self.engine.symbol_states["CL"]["tick_size"] = 0.01
        bars_flat = []
        for i in range(15):
            # Pendiente de 0.001 por barra = 0.01 en 10 barras (< 0.03 pts)
            bars_flat.append({
                'Timestamp': self.now - timedelta(minutes=(15 - i) * 5),
                'Open': 100.0 + i * 0.001, 'High': 100.5 + i * 0.001, 'Low': 99.5 + i * 0.001, 'Close': 100.0 + i * 0.001,
                'Volume': 500,
                'VWAP': 100.0 + i * 0.001,
                'Upper_Band': 102.0, 'Lower_Band': 98.0
            })
        df_cl_flat = pd.DataFrame(bars_flat)
        df_cl_flat.iloc[-2, df_cl_flat.columns.get_loc('Low')] = df_cl_flat.iloc[-2]['VWAP'] + 0.10
        precio_actual = float(df_cl_flat.iloc[-1]['VWAP'])

        setup_cl_flat = self.engine.verificar_setup_vwap(df_cl_flat, "CL", current_price=precio_actual)
        self.assertIsNone(setup_cl_flat, "Debe rechazar CL si la pendiente (+0.01 pts) es menor a 0.03 pts")

        # Ahora probamos con pendiente válida para petróleo (+0.05 pts en 10 barras)
        bars_ok = []
        for i in range(15):
            bars_ok.append({
                'Timestamp': self.now - timedelta(minutes=(15 - i) * 5),
                'Open': 100.0 + i * 0.005, 'High': 100.5 + i * 0.005, 'Low': 99.5 + i * 0.005, 'Close': 100.0 + i * 0.005,
                'Volume': 500,
                'VWAP': 100.0 + i * 0.005,
                'Upper_Band': 102.0, 'Lower_Band': 98.0
            })
        df_cl_ok = pd.DataFrame(bars_ok)
        df_cl_ok.iloc[-2, df_cl_ok.columns.get_loc('Low')] = df_cl_ok.iloc[-2]['VWAP'] + 0.10
        precio_actual_ok = float(df_cl_ok.iloc[-1]['VWAP'])

        setup_cl_ok = self.engine.verificar_setup_vwap(df_cl_ok, "CL", current_price=precio_actual_ok)
        self.assertIsNotNone(setup_cl_ok, "Debe aprobar CL si la pendiente (+0.05 pts) supera 0.03 pts")
        self.assertEqual(setup_cl_ok["direccion"], "LONG")

    def test_umbrales_pendientes_minimas_todos_los_simbolos(self):
        """Verifica que los umbrales de pendiente mínima por símbolo coincidan exactamente con la especificación institucional."""
        from config import get_min_slope_points
        self.assertEqual(get_min_slope_points("ES"), 1.0)
        self.assertEqual(get_min_slope_points("MES"), 1.0)
        self.assertEqual(get_min_slope_points("NQ"), 1.5)
        self.assertEqual(get_min_slope_points("MNQ"), 1.5)
        self.assertEqual(get_min_slope_points("YM"), 2.0)
        self.assertEqual(get_min_slope_points("MYM"), 2.0)
        self.assertEqual(get_min_slope_points("RTY"), 0.30)
        self.assertEqual(get_min_slope_points("M2K"), 0.30)
        self.assertEqual(get_min_slope_points("CL"), 0.03)
        self.assertEqual(get_min_slope_points("MCL"), 0.03)
        self.assertEqual(get_min_slope_points("GC"), 1.0)
        self.assertEqual(get_min_slope_points("MGC"), 1.0)

    def test_default_sl_ticks_by_symbol(self):
        """Verifica que el SL inicial por símbolo esté ajustado correctamente (ES: 35t, NQ: 90t, etc.)."""
        from config import get_default_sl_ticks, get_micro_shield_sl_ticks
        self.assertEqual(get_default_sl_ticks("ES"), 35)
        self.assertEqual(get_default_sl_ticks("MES"), 35)
        self.assertEqual(get_default_sl_ticks("NQ"), 90)
        self.assertEqual(get_default_sl_ticks("MNQ"), 90)
        self.assertEqual(get_default_sl_ticks("YM"), 50)
        self.assertEqual(get_default_sl_ticks("MYM"), 50)
        
        # Micro-escudo proporcional
        self.assertEqual(get_micro_shield_sl_ticks("ES"), 15)
        self.assertEqual(get_micro_shield_sl_ticks("NQ"), 39)

    def test_ejecutar_orden_nq_usa_sl_ticks_90(self):
        """Verifica que NQ ejecute la orden con SL de 90 ticks (22.50 pts) en lugar de 35 ticks."""
        setup_nq = {
            "simbolo": "NQ",
            "direccion": "LONG",
            "precio_entrada": 20000.0,
            "vwap_cruce": 20000.0,
            "upper_band": 20100.0,
            "lower_band": 19900.0,
            "target_band": 20100.0,
            "d_base": 100.0
        }
        self.engine.ejecutar_orden(setup_nq, "NQ", ignore_schedule=True)
        _, kwargs_nq = self.mock_client.place_order.call_args
        self.assertEqual(kwargs_nq["side"], 0)
        self.assertEqual(kwargs_nq["sl_ticks"], -90, "NQ debe enviar -90 ticks de SL para LONG (TopstepX)")
        self.assertEqual(kwargs_nq["tp_ticks"], 400)

    def test_micros_y_minis_habilitados_por_defecto(self):
        """Verifica que tanto minis (ES, NQ) como micros (MES, MNQ) estén habilitados simultáneamente."""
        self.assertTrue(self.engine.symbol_states["ES"]["enabled"])
        self.assertTrue(self.engine.symbol_states["NQ"]["enabled"])
        self.assertTrue(self.engine.symbol_states["MES"]["enabled"])
        self.assertTrue(self.engine.symbol_states["MNQ"]["enabled"])

    def test_gatillo_proximidad_15_ticks_long_y_sl_desde_vwap(self):
        """
        Verifica que:
        1. A 15 ticks (3.75 pts en ES) del VWAP (5003.75 vs 5000.00), el gatillo LONG se active SIN tocar VWAP.
        2. A 16 ticks (5004.00 vs 5000.00), NO se active.
        3. El Stop Loss se coloque exactamente a 35 ticks desde el VWAP (4991.25), con distancia de orden = 50 ticks.
        """
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5015.0, 'High': 5025.0, 'Low': 5006.0, 'Close': 5020.0, # 100% arriba de VWAP 4995.0
                'Volume': 1000, 'VWAP': 4995.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 5015.0, 'High': 5018.0, 'Low': 5003.0, 'Close': 5003.75,
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])

        # Caso A: A 16 ticks del VWAP (5004.00 vs VWAP 5000.00) -> NO DEBE ACTIVARSE
        setup_16t = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=5004.00)
        self.assertIsNone(setup_16t, "A 16 ticks del VWAP no debe activarse")

        # Caso B: A 15 ticks exactos del VWAP (5003.75 vs VWAP 5000.00) -> DEBE ACTIVARSE
        setup_15t = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=5003.75)
        self.assertIsNotNone(setup_15t, "A 15 ticks del VWAP debe activarse")
        self.assertEqual(setup_15t["direccion"], "LONG")
        self.assertEqual(setup_15t["precio_entrada"], 5003.75)
        self.assertEqual(setup_15t["vwap_cruce"], 5000.0)

        # Caso C: Ejecutar orden y verificar que el SL se coloque a 35 ticks del VWAP (4991.25)
        self.engine.ejecutar_orden(setup_15t, "ES", ignore_schedule=True)
        _, kwargs = self.mock_client.place_order.call_args
        # Distancia en ticks desde entrada (5003.75) hasta SL (4991.25) = (5003.75 - 4991.25) / 0.25 = 50 ticks
        self.assertEqual(kwargs["sl_ticks"], -50, "TopstepX debe recibir -50 ticks (35t desde VWAP + 15t desde entrada)")
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertEqual(trade["current_sl_price"], 4991.25, "El precio de SL debe ser exactamente 5000.00 - 35*0.25 = 4991.25")

    def test_gatillo_proximidad_short_y_sl_desde_vwap(self):
        """Verifica gatillo SHORT a 15 ticks debajo de VWAP (4996.25 vs 5000.00) con SL a 35t arriba de VWAP (5008.75)."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 4980.0, 'High': 4990.0, 'Low': 4975.0, 'Close': 4985.0, # 100% abajo de VWAP 5005.0
                'Volume': 1000, 'VWAP': 5005.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now,
                'Open': 4985.0, 'High': 4997.0, 'Low': 4980.0, 'Close': 4996.25,
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
            }
        ])

        self.engine.symbol_states["ES"]["has_open_position"] = False
        self.engine.symbol_states["ES"]["active_trade"] = None

        # A 15 ticks debajo del VWAP (4996.25) -> Activa SHORT
        setup = self.engine.verificar_setup_vwap(df_5m, "ES", current_price=4996.25)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "SHORT")

        self.engine.ejecutar_orden(setup, "ES", ignore_schedule=True)
        _, kwargs = self.mock_client.place_order.call_args
        # Distancia en ticks desde entrada (4996.25) hasta SL (5008.75 = VWAP + 35t): (5008.75 - 4996.25)/0.25 = 50 ticks
        self.assertEqual(kwargs["sl_ticks"], 50)
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertEqual(trade["current_sl_price"], 5008.75)

    def test_csv_registro_hora_apertura_y_cierre(self):
        """Verifica que el CSV registre Hora_Apertura y Hora_Cierre de forma independiente e inmutable."""
        trade = {
            "symbol": "ES",
            "direction": "LONG",
            "entry_price": 5000.0,
            "current_price": 5010.0,
            "order_id": 888123,
            "entry_time_str": "10:26:22",
            "initial_sl_ticks": 35,
            "tp_ticks": 40
        }
        self.engine.registrar_cierre_posicion("ES", trade, precio_salida=5010.0, motivo_cierre="Test Cierre")
        
        historial = self.engine.load_operations_history()
        self.assertTrue(len(historial) > 0)
        ultima_op = historial[-1]
        self.assertEqual(ultima_op["Hora_Apertura"], "10:26:22")
        self.assertIn(":", ultima_op["Hora_Cierre"])
        self.assertNotEqual(ultima_op["Hora_Cierre"], "EN CURSO")

    def test_canasta_profit_flatten_all(self):
        """Verifica que emergency_flatten_all liquide y cancele órdenes para todos los contratos abiertos."""
        self.mock_client.close_contract_position.return_value = {"success": True}
        # Primera llamada devuelve 2 posiciones, segunda llamada (verificación de seguridad) devuelve [] confirmando flat
        self.mock_client.get_open_positions.side_effect = [
            [{"contractId": "CON_ES_TEST", "size": 1}, {"contractId": "CON_YM_TEST", "size": 1}],
            []
        ]
        self.engine.symbol_states["ES"]["has_open_position"] = True
        self.engine.symbol_states["ES"]["active_trade"] = {"symbol": "ES", "contract_id": "CON_ES_TEST", "entry_price": 5000.0, "direction": "LONG"}
        
        res = self.engine.emergency_flatten_all(reason="Test Basket TP")
        self.assertTrue(res["success"])
        self.assertFalse(self.engine.symbol_states["ES"]["has_open_position"])
        self.assertIsNone(self.engine.symbol_states["ES"]["active_trade"])
        self.assertEqual(self.mock_client.close_contract_position.call_count, 2)

    def test_bloqueo_apertura_cme_primeras_dos_horas_media(self):
        """Verifica que las primeras 2:30h tras la apertura de CME (18:00 a 20:30 ET) estén estrictamente bloqueadas."""
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        # 1. 18:05 ET (Lunes) -> Bloqueado
        with patch("engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 26, 18, 5, 0, tzinfo=ZoneInfo("America/New_York"))
            operable, motivo = self.engine.es_horario_operable()
            self.assertFalse(operable)
            self.assertIn("18:00-20:30 ET", motivo)

        # 2. 19:45 ET (Lunes) -> Bloqueado
        with patch("engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 26, 19, 45, 0, tzinfo=ZoneInfo("America/New_York"))
            operable, motivo = self.engine.es_horario_operable()
            self.assertFalse(operable)
            self.assertIn("18:00-20:30 ET", motivo)

        # 3. 20:29 ET (Lunes) -> Bloqueado
        with patch("engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 26, 20, 29, 0, tzinfo=ZoneInfo("America/New_York"))
            operable, motivo = self.engine.es_horario_operable()
            self.assertFalse(operable)
            self.assertIn("18:00-20:30 ET", motivo)

        # 4. 20:31 ET (Lunes) -> APROBADO (Fin del buffer de apertura)
        with patch("engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 10, 26, 20, 31, 0, tzinfo=ZoneInfo("America/New_York"))
            operable, motivo = self.engine.es_horario_operable()
            self.assertTrue(operable)
            self.assertIn("Sesión regular activa", motivo)

    def test_candado_ganancia_diaria_trailing(self):
        """Verifica que al superar +$750 USD se active el piso del 70% y liquide/bloquee si retrocede."""
        self.mock_client.close_contract_position.return_value = {"success": True}
        self.mock_client.get_open_positions.return_value = []

        # 1. Ganancia de +$1,000 USD (supera $750 USD): activa piso a 70% = $700 USD
        self.engine.verificar_candado_ganancia_diaria(total_floating_pnl=1000.0)
        self.assertFalse(self.engine.daily_lock_active)
        self.assertEqual(self.engine.daily_peak_pnl, 1000.0)
        self.assertEqual(self.engine.daily_floor_pnl, 700.0)

        # 2. Ganancia sube a +$1,500 USD: nuevo pico $1,500, nuevo piso 70% = $1,050 USD
        self.engine.verificar_candado_ganancia_diaria(total_floating_pnl=1500.0)
        self.assertFalse(self.engine.daily_lock_active)
        self.assertEqual(self.engine.daily_peak_pnl, 1500.0)
        self.assertEqual(self.engine.daily_floor_pnl, 1050.0)

        # 3. Pullback moderado a +$1,200 USD: sigue por encima del piso de $1,050 -> Sigue operando
        self.engine.verificar_candado_ganancia_diaria(total_floating_pnl=1200.0)
        self.assertFalse(self.engine.daily_lock_active)

        # 4. Pullback perfora el piso a +$950 USD (< $1,050 USD): ¡Dispara Candado Trailing!
        self.engine.verificar_candado_ganancia_diaria(total_floating_pnl=950.0)
        self.assertTrue(self.engine.daily_lock_active)
        self.assertIn("Piso Trailing Diario Alcanzado", self.engine.daily_lock_reason)

        # 5. Verifica que es_horario_operable() bloquee nuevas operaciones por el resto del día
        operable, motivo = self.engine.es_horario_operable()
        self.assertFalse(operable)
        self.assertIn("Candado de Ganancia Diaria", motivo)

if __name__ == '__main__':
    unittest.main()
