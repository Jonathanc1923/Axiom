import unittest
from datetime import datetime, timedelta, timezone
import pandas as pd
from unittest.mock import MagicMock
from engine import AxiomEngine
from config import USE_VWAP_TREND_FILTER

class TestVWAPTrendFilter(unittest.TestCase):
    def setUp(self):
        mock_client = MagicMock()
        self.engine = AxiomEngine(mock_client)
        self.now = datetime.now(timezone.utc)

    def test_trend_filter_short_approved_when_vwap_falling(self):
        """SHORT debe ser aprobado si el VWAP de la sesión está en bajada."""
        # VWAP baja de 5002 a 5000 (delta = -2.0)
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4995.0, 'Close': 5001.0,
                'Volume': 1000, 'VWAP': 5002.0, 'Upper_Band': 5040.0, 'Lower_Band': 4964.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4995.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 4997.0, # mecha tocó VWAP (5000), cuerpo bajo VWAP, dist=3 pts (12t)
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        # 12 ticks confirmacion, max corredor = 25% de 38 = 9.5 pts = 38 ticks
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
        self.assertIsNotNone(setup, "Debe aprobar el setup SHORT porque VWAP está en bajada")
        self.assertEqual(setup["direccion"], "SHORT")
        self.assertLess(setup["vwap_delta"], 0, "vwap_delta debe ser negativo")
        self.assertEqual(setup["vwap_trend"], "FALLING")

    def test_trend_filter_short_rejected_when_vwap_rising(self):
        """SHORT debe ser RECHAZADO si el VWAP de la sesión está en subida."""
        # VWAP sube de 4998 a 5000 (delta = +2.0)
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 4995.0, 'High': 5000.0, 'Low': 4990.0, 'Close': 4996.0,
                'Volume': 1000, 'VWAP': 4998.0, 'Upper_Band': 5036.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4995.0, 'High': 5001.0, 'Low': 4990.0, 'Close': 4997.0, # mecha tocó VWAP
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
        self.assertIsNone(setup, "Debe RECHAZAR el setup SHORT porque VWAP está en subida")

    def test_trend_filter_long_approved_when_vwap_rising(self):
        """LONG debe ser aprobado si el VWAP de la sesión está en subida."""
        # VWAP sube de 4998 a 5000 (delta = +2.0)
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 4995.0, 'High': 5000.0, 'Low': 4990.0, 'Close': 4996.0,
                'Volume': 1000, 'VWAP': 4998.0, 'Upper_Band': 5036.0, 'Lower_Band': 4960.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4999.0, 'Close': 5003.0, # mecha tocó VWAP (5000), cuerpo sobre VWAP, dist=3 pts (12t)
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
        self.assertIsNotNone(setup, "Debe aprobar el setup LONG porque VWAP está en subida")
        self.assertEqual(setup["direccion"], "LONG")
        self.assertGreater(setup["vwap_delta"], 0, "vwap_delta debe ser positivo")
        self.assertEqual(setup["vwap_trend"], "RISING")

    def test_trend_filter_long_rejected_when_vwap_falling(self):
        """LONG debe ser RECHAZADO si el VWAP de la sesión está en bajada."""
        # VWAP baja de 5002 a 5000 (delta = -2.0)
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4995.0, 'Close': 5001.0,
                'Volume': 1000, 'VWAP': 5002.0, 'Upper_Band': 5040.0, 'Lower_Band': 4964.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4999.0, 'Close': 5003.0, # mecha tocó VWAP
                'Volume': 1500, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-1)
        self.assertIsNone(setup, "Debe RECHAZAR el setup LONG porque VWAP está en bajada")

    def test_trend_filter_with_bar_index_minus_2(self):
        """Verifica que la evaluación de la vela recién cerrada (bar_index=-2) calcule la tendencia correctamente."""
        bars = [
            {
                'Timestamp': self.now - timedelta(minutes=90),
                'Open': 5005.0, 'High': 5010.0, 'Low': 4995.0, 'Close': 5001.0,
                'Volume': 1000, 'VWAP': 5004.0, 'Upper_Band': 5040.0, 'Lower_Band': 4964.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=60),
                'Open': 4995.0, 'High': 5002.0, 'Low': 4990.0, 'Close': 4998.0, # mecha tocó VWAP (5001), cuerpo bajo VWAP, dist=3 pts (12t)
                'Volume': 1500, 'VWAP': 5001.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            },
            {
                'Timestamp': self.now - timedelta(minutes=30),
                'Open': 4997.0, 'High': 5005.0, 'Low': 4995.0, 'Close': 5000.0,
                'Volume': 800, 'VWAP': 5000.0, 'Upper_Band': 5038.0, 'Lower_Band': 4962.0
            }
        ]
        df = pd.DataFrame(bars)
        # Evaluando bar_index=-2 (la vela de las -60m, cuyo VWAP era 5001 vs 5004 anterior -> Bajando)
        setup = self.engine.verificar_setup_vwap(df, "ES", bar_index=-2)
        self.assertIsNotNone(setup, "Debe aprobar el setup SHORT en bar_index=-2")
        self.assertEqual(setup["direccion"], "SHORT")
        self.assertEqual(setup["vwap_trend"], "FALLING")
        self.assertEqual(setup["vwap_delta"], -3.0)

if __name__ == "__main__":
    unittest.main()
