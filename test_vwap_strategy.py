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
        """Vela previa 100% por encima de VWAP, precio actual toca/cruza VWAP -> Señal LONG."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5015.0, 'High': 5020.0, 'Low': 5005.0, 'Close': 5010.0,
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
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

    def test_setup_short_valid(self):
        """Vela previa 100% por debajo de VWAP, precio actual toca/cruza VWAP -> Señal SHORT."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 4985.0, 'High': 4995.0, 'Low': 4980.0, 'Close': 4990.0,
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
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

    def test_setup_rechazado_si_vela_previa_toco_vwap(self):
        """Si la vela previa tocó o cruzó el VWAP, el contexto NO es válido -> None."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5010.0, 'High': 5015.0, 'Low': 4999.0, 'Close': 5005.0, # Low < VWAP
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
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
        """Verifica que el bot abra igual si toca o pasa el VWAP (sin restricción anti-chasing)."""
        df_5m = pd.DataFrame([
            {
                'Timestamp': self.now - timedelta(minutes=5),
                'Open': 5015.0, 'High': 5020.0, 'Low': 5005.0, 'Close': 5010.0,
                'Volume': 1000, 'VWAP': 5000.0, 'Upper_Band': 5040.0, 'Lower_Band': 4960.0
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
        self.assertEqual(kwargs_long["sl_ticks"], -25, "TopstepX exige signo negativo para SL en LONG")
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
        self.assertEqual(kwargs_short["sl_ticks"], 25, "TopstepX exige signo positivo para SL en SHORT")
        self.assertEqual(kwargs_short["tp_ticks"], -160, "TopstepX exige signo negativo para TP en SHORT")

    def test_escalera_de_hitos_long(self):
        """Prueba los hitos en LONG: 40% (BE+1), 70% (SL 45%, TP 140%), 120% (SL 108%, TP 170%)."""
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
        
        # 1. Al 20% (Precio 5008.0): Aún no alcanza el 40% -> Sin cambios (stage 0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5008.0)
        self.assertEqual(trade["sl_stage"], 0)
        
        # 2. Al 45% (Precio 5018.0 >= 40% de 40.0 = 16.0 pts): Hito 1 -> SL a BE + 1 tick (5000.25)
        self.engine.monitorear_posicion_abierta("ES", current_price=5018.0)
        self.assertEqual(trade["sl_stage"], 1)
        self.assertEqual(trade["current_sl_price"], 5000.25)
        
        # 3. Al 75% (Precio 5030.0 >= 70% de 40.0 = 28.0 pts): Hito 2 -> SL a 45% (5018.0), TP a 140% (5056.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5030.0)
        self.assertEqual(trade["sl_stage"], 2)
        self.assertEqual(trade["current_sl_price"], 5018.0)
        self.assertEqual(trade["current_tp_price"], 5056.0)
        
        # 4. Al 125% (Precio 5050.0 >= 120% de 40.0 = 48.0 pts): Hito 3 -> SL a 108% (5043.25), TP a 170% (5068.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5050.0)
        self.assertEqual(trade["sl_stage"], 3)
        self.assertAlmostEqual(trade["current_sl_price"], 5043.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5068.0)

        # 5. Al 136% (Precio 5054.5 >= 135% de 40.0 = 54.0 pts): Hito 4 -> SL a 128% (5051.25), TP a 200% (5080.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5054.5)
        self.assertEqual(trade["sl_stage"], 4)
        self.assertAlmostEqual(trade["current_sl_price"], 5051.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5080.0)

    def test_escalera_de_hitos_short(self):
        """Prueba los hitos en SHORT: sin UnboundLocalError y calculando correctamente ganancia."""
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
        
        # 1. Al 20% (Precio 4992.0): Sin cambios (stage 0)
        self.engine.monitorear_posicion_abierta("ES", current_price=4992.0)
        self.assertEqual(trade["sl_stage"], 0)
        
        # 2. Al 45% (Precio 4982.0 <= 5000 - 16.0 = 4984.0): Hito 1 -> SL a BE - 1 tick (4999.75)
        self.engine.monitorear_posicion_abierta("ES", current_price=4982.0)
        self.assertEqual(trade["sl_stage"], 1)
        self.assertEqual(trade["current_sl_price"], 4999.75)
        
        # 3. Al 75% (Precio 4970.0 <= 5000 - 28.0 = 4972.0): Hito 2 -> SL a 45% (4982.0), TP a 140% (4944.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=4970.0)
        self.assertEqual(trade["sl_stage"], 2)
        self.assertEqual(trade["current_sl_price"], 4982.0)
        self.assertEqual(trade["current_tp_price"], 4944.0)

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

        # 3. Avance a Hito 2 (5030.0): SL a 45%, TP a 140%
        self.engine.monitorear_posicion_abierta("ES", current_price=5030.0)
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1, "Debe seguir existiendo exactamente 1 fila")
        self.assertIn("Hito 2", history[0]["Estado"])
        self.assertIn("Hito 2", history[0]["Hito_Alcanzado"])
        self.assertEqual(history[0]["Max_Pct_TP"], "75.0%")

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
        
        # Avanzar a Hito 2 (5030.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5030.0)
        trade = self.engine.symbol_states["ES"]["active_trade"]
        self.assertEqual(trade["sl_stage"], 2)
        
        # Simular cierre en mercado: la posición ya no existe en TopstepX (>5 seg)
        trade["entry_timestamp"] = trade["entry_timestamp"] - 10.0
        self.mock_client.get_open_positions.return_value = []
        
        self.engine.monitorear_posicion_abierta("ES", current_price=5030.0)
        
        # active_trade debe haber concluido
        self.assertIsNone(self.engine.symbol_states["ES"]["active_trade"])
        self.assertFalse(self.engine.symbol_states["ES"]["has_open_position"])
        
        # En CSV debe figurar CERRADA con PnL y el Hito 2 preservado
        history = self.engine.load_operations_history()
        self.assertEqual(len(history), 1)
        self.assertIn("CERRADA", history[0]["Estado"])
        self.assertIn("Hito 2", history[0]["Hito_Alcanzado"])
        self.assertEqual(history[0]["Max_Pct_TP"], "75.0%")
        self.assertEqual(history[0]["Precio_Salida_Real"], "5030.00")
        pnl_net = float(history[0]["PnL_Neto_USD"])
        self.assertGreater(pnl_net, 0) # 30 pts * $50 - $3.78 = $1496.22

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

if __name__ == '__main__':
    unittest.main()
