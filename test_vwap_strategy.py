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
        
        self.engine = AxiomEngine(self.mock_client, default_symbol="ES")
        self.engine.csv_path = self.temp_csv.name
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

    def test_ejecutar_orden_sl_25_ticks_y_tp_banda(self):
        """Verifica que la orden se envía con 25 ticks de SL y TP a la banda, y congela D_base."""
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
        self.assertEqual(trade["entry_price"], 5000.0)
        self.assertEqual(trade["initial_sl_ticks"], 25)
        self.assertEqual(trade["initial_sl_price"], 5000.0 - (25 * 0.25)) # 4993.75
        self.assertEqual(trade["base_tp_pts"], 40.0)
        self.assertEqual(trade["sl_stage"], 0)
        
        # Verificar llamada a TopstepClient
        self.mock_client.place_order.assert_called_once()
        args, kwargs = self.mock_client.place_order.call_args
        self.assertEqual(kwargs["sl_ticks"], 25)
        self.assertEqual(kwargs["tp_ticks"], 160) # 40.0 / 0.25 = 160 ticks

    def test_escalera_de_hitos_40_70_120(self):
        """Prueba los 3 hitos: 40% (BE+1), 70% (SL 45%, TP 120%), 120% (SL 108%, TP 150%)."""
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
        
        # 3. Al 75% (Precio 5030.0 >= 70% de 40.0 = 28.0 pts): Hito 2 -> SL a 45% (18.0 pts = 5018.0), TP a 120% (48.0 pts = 5048.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5030.0)
        self.assertEqual(trade["sl_stage"], 2)
        self.assertEqual(trade["current_sl_price"], 5018.0)
        self.assertEqual(trade["current_tp_price"], 5048.0)
        
        # 4. Al 125% (Precio 5050.0 >= 120% de 40.0 = 48.0 pts): Hito 3 -> SL a 108% (43.25 pts aprox), TP a 150% (60.0 pts = 5060.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5050.0)
        self.assertEqual(trade["sl_stage"], 3)
        self.assertAlmostEqual(trade["current_sl_price"], 5043.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5060.0)

        # 5. Al 136% (Precio 5054.5 >= 135% de 40.0 = 54.0 pts): Hito 4 -> SL a 128% (51.25 pts = 5051.25), TP a 170% (68.0 pts = 5068.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5054.5)
        self.assertEqual(trade["sl_stage"], 4)
        self.assertAlmostEqual(trade["current_sl_price"], 5051.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5068.0)

        # 6. Al 156% (Precio 5062.5 >= 155% de 40.0 = 62.0 pts): Hito 5 (Infinito) -> SL a 148% (59.25 pts = 5059.25), TP a 190% (76.0 pts = 5076.0)
        self.engine.monitorear_posicion_abierta("ES", current_price=5062.5)
        self.assertEqual(trade["sl_stage"], 5)
        self.assertAlmostEqual(trade["current_sl_price"], 5059.25, places=2)
        self.assertEqual(trade["current_tp_price"], 5076.0)

if __name__ == '__main__':
    unittest.main()

