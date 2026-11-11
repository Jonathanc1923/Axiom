import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pandas as pd
from engine import AxiomEngine

class TestVWAPStrategyInversionAndTrend(unittest.TestCase):
    def setUp(self):
        mock_client = MagicMock()
        self.engine = AxiomEngine(mock_client)
        self.now = datetime.now(timezone.utc)

    def test_standard_mode_short(self):
        """Modo Estándar: Setup SHORT abre SHORT con SL a 5t de VWAP y TP a banda."""
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4995.0, 'Close': 5001.0,
                'Volume': 1000, 'VWAP': 5002.0, 'Upper_Band': 5040.0, 'Lower_Band': 4964.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4995.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 4997.0, # mecha tocó VWAP (5000), cuerpo bajo VWAP, dist=3 pts (12t)
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0 # std_dev = 38 pts
            }
        ]
        df = pd.DataFrame(bars)
        with patch("engine.INVERT_STRATEGY", False):
            setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
            self.assertIsNotNone(setup)
            self.assertEqual(setup["direccion"], "SHORT")
            self.assertEqual(setup["setup_base"], "SHORT")
            self.assertFalse(setup["es_invertido"])
            # SL por encima de VWAP (5000 + 5*0.25 = 5001.25)
            self.assertEqual(setup["precio_sl"], 5001.25)
            # TP hacia banda inferior (4997 - 50% de 38 = 4997 - 19 = 4978.0)
            self.assertEqual(setup["precio_tp"], 4978.0)
            # SL es pequeño (17 ticks), TP es grande (76 ticks)
            self.assertLess(setup["sl_ticks"], setup["tp_ticks"])

    def test_inverted_mode_short_becomes_long_with_swapped_brackets(self):
        """Modo Invertido: Setup SHORT ejecuta LONG con TP donde estaba SL y SL donde estaba TP."""
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4995.0, 'Close': 5001.0,
                'Volume': 1000, 'VWAP': 5002.0, 'Upper_Band': 5040.0, 'Lower_Band': 4964.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4995.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 4997.0,
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        with patch("engine.INVERT_STRATEGY", True):
            setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
            self.assertIsNotNone(setup)
            # Dirección invertida a LONG
            self.assertEqual(setup["direccion"], "LONG")
            self.assertEqual(setup["setup_base"], "SHORT")
            self.assertTrue(setup["es_invertido"])
            # DONDE TENÍAMOS SL AHORA ES TP:
            # El antiguo SL (5001.25) es ahora el TAKE PROFIT (ganancia pequeña de 17 ticks)
            self.assertEqual(setup["precio_tp"], 5001.25)
            self.assertGreater(setup["precio_tp"], setup["precio_entrada"])
            # Y DONDE TENÍAMOS TP AHORA ES SL:
            # El antiguo TP (4978.00) es ahora el STOP LOSS (pérdida grande de 76 ticks)
            self.assertEqual(setup["precio_sl"], 4978.00)
            self.assertLess(setup["precio_sl"], setup["precio_entrada"])
            # Gana poco cuando gana, pierde mucho cuando pierde
            self.assertLess(setup["tp_ticks"], setup["sl_ticks"])
            self.assertEqual(setup["tp_ticks"], 17)
            self.assertEqual(setup["sl_ticks"], 76)

    def test_inverted_mode_long_becomes_short_with_swapped_brackets(self):
        """Modo Invertido: Setup LONG ejecuta SHORT con TP donde estaba SL y SL donde estaba TP."""
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 4995.0, 'High': 5000.0, 'Low': 4990.0, 'Close': 4996.0,
                'Volume': 1000, 'VWAP': 4998.0, 'Upper_Band': 5036.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4999.0, 'Close': 5003.0, # mecha tocó VWAP (5000), cuerpo sobre VWAP
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        with patch("engine.INVERT_STRATEGY", True):
            setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
            self.assertIsNotNone(setup)
            # Dirección invertida a SHORT
            self.assertEqual(setup["direccion"], "SHORT")
            self.assertEqual(setup["setup_base"], "LONG")
            self.assertTrue(setup["es_invertido"])
            # DONDE TENÍAMOS SL AHORA ES TP:
            # Antiguo SL (5000 - 5*0.25 = 4998.75) es ahora el TAKE PROFIT (ganancia pequeña)
            self.assertEqual(setup["precio_tp"], 4998.75)
            self.assertLess(setup["precio_tp"], setup["precio_entrada"])
            # Y DONDE TENÍAMOS TP AHORA ES SL:
            # Antiguo TP (5003 + 19 = 5022.00) es ahora el STOP LOSS (pérdida grande)
            self.assertEqual(setup["precio_sl"], 5022.00)
            self.assertGreater(setup["precio_sl"], setup["precio_entrada"])
            # Gana poco cuando gana, pierde mucho cuando pierde
            self.assertLess(setup["tp_ticks"], setup["sl_ticks"])
            self.assertEqual(setup["tp_ticks"], 17)
            self.assertEqual(setup["sl_ticks"], 76)

    def test_trend_filter_rejection_still_active(self):
        """El filtro de tendencia sigue activo para evitar entradas contra la dirección del VWAP."""
        # VWAP subiendo pero setup intenta LONG en vela bajista
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 4995.0, 'High': 5000.0, 'Low': 4990.0, 'Close': 4996.0,
                'Volume': 1000, 'VWAP': 4998.0, 'Upper_Band': 5036.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4995.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 4997.0, # mecha tocó VWAP por debajo
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
        self.assertIsNone(setup, "Debe rechazar porque VWAP sube y el setup base es bajista")

if __name__ == "__main__":
    unittest.main()
