import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
from unittest.mock import MagicMock

from engine import AxiomEngine
from topstep_client import TopstepClient

class TestAbsorcionVWAP(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock(spec=TopstepClient)
        self.engine = AxiomEngine(self.mock_client, default_symbol="NQ")
        self.engine.symbol_states["NQ"]["tick_size"] = 0.25
        self.engine.symbol_states["NQ"]["contract_id"] = "CON_NQ_TEST"
        
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
        """Prueba setup LONG: Vol +15% sobre SMA10, Low en zona VWAP, rebote y cierre arriba."""
        df_1m = self._crear_df_1m(
            vol_cerrada=1150,  # 15% arriba del promedio (1000)
            open_p=20002.0,
            high_p=20003.0,
            low_p=19999.50,   # Entró a zona VWAP (20000 + 3t = 20000.75)
            close_p=20001.50   # Cerró por encima de VWAP y mecha inf = 2.0 pts (8 ticks >= 2 ticks)
        )
        setup = self.engine.verificar_absorcion_vwap_1m(self.df_5m, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "LONG")
        self.assertEqual(setup["extremo_precio"], 19999.50)
        self.assertEqual(setup["volumen_1m"], 1150)
        self.assertGreaterEqual(setup["rvol"], 1.10)

        # Verificar cálculo de SL (10 ticks debajo del extremo 19999.50)
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20001.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0
        )
        self.assertEqual(precio_sl, 19997.00)  # 19999.50 - 2.50 = 19997.00
        self.assertEqual(ticks_sl, -18)        # (20001.50 - 19997.00) / 0.25 = 18 ticks
        self.assertGreaterEqual(abs(ticks_tp), 10)

    def test_short_absorcion_detectada(self):
        """Prueba setup SHORT: Vol +20% sobre SMA10, High en zona VWAP, rechazo y cierre abajo."""
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
            high_p=20000.50,   # Entró a zona VWAP (20000 - 3t = 19999.25)
            low_p=19997.0,
            close_p=19998.50   # Cerró por debajo de VWAP y mecha sup = 2.0 pts (8 ticks >= 2 ticks)
        )
        setup = self.engine.verificar_absorcion_vwap_1m(df_5m_short, "NQ", df_1m_override=df_1m)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["direccion"], "SHORT")
        self.assertEqual(setup["extremo_precio"], 20000.50)
        self.assertEqual(setup["volumen_1m"], 1200)

        # Verificar cálculo de SL (10 ticks encima del extremo 20000.50)
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "SHORT", 19998.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0
        )
        self.assertEqual(precio_sl, 20003.00)  # 20000.50 + 2.50 = 20003.00
        self.assertEqual(ticks_sl, 18)         # (20003.00 - 19998.50) / 0.25 = 18 ticks (positivo en SHORT)

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
        """Si hubo mucho volumen pero el precio perforó y cerró abajo (ruptura), no debe operar LONG."""
        df_1m = self._crear_df_1m(
            vol_cerrada=1500,  # Esfuerzo alto (+50%)
            open_p=20002.0,
            high_p=20003.0,
            low_p=19996.00,
            close_p=19997.00   # Cerró 3 puntos (12 ticks) por debajo del VWAP -> RUPTURA!
        )
        setup = self.engine.verificar_absorcion_vwap_1m(self.df_5m, "NQ", df_1m_override=df_1m)
        self.assertIsNone(setup, "No debe operar LONG si la vela cerró rompiendo el VWAP hacia abajo")

    def test_fuera_de_zona_ignora(self):
        """Si el precio de la vela 5m no llegó a la zona de VWAP +- 3 ticks, no debe operar."""
        df_5m_lejos = pd.DataFrame([{
            'Timestamp': self.now,
            'Open': 20020.0,
            'High': 20025.0,
            'Low': 20015.0,    # A 15 puntos (60 ticks) del VWAP
            'Close': 20022.0,
            'Volume': 5000,
            'VWAP': 20000.0,
            'Upper_Band': 20040.0,
            'Lower_Band': 19960.0
        }])
        setup = self.engine.verificar_absorcion_vwap_1m(df_5m_lejos, "NQ")
        self.assertIsNone(setup)

if __name__ == '__main__':
    unittest.main()
