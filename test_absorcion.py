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

        # SL a 5 ticks debajo del extremo 19999.50 (19999.50 - 1.25 = 19998.25)
        # TP al 15% de volumen = 15 ticks (3.75 pts -> 20001.50 + 3.75 = 20005.25)
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20001.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0, rvol=setup["rvol"]
        )
        self.assertEqual(precio_sl, 19998.25)  # 19999.50 - 5 * 0.25 = 19998.25
        self.assertEqual(ticks_sl, -13)        # (20001.50 - 19998.25) / 0.25 = 13 ticks
        self.assertEqual(ticks_tp, 15)         # 15 ticks por +15% de volumen
        self.assertEqual(precio_tp, 20005.25)

    def test_short_absorcion_detectada(self):
        """Prueba setup SHORT: Vol +20%, SL a 5 ticks del extremo, TP dinámico por volumen."""
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

        # SL a 5 ticks encima del extremo 20000.50 (20000.50 + 1.25 = 20001.75)
        # TP al 20% de volumen = -20 ticks
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "SHORT", 19998.50, setup["extremo_precio"], 20040.0, 19960.0, "NQ", 20000.0, rvol=setup["rvol"]
        )
        self.assertEqual(precio_sl, 20001.75)  # 20000.50 + 5 * 0.25 = 20001.75
        self.assertEqual(ticks_sl, 13)         # (20001.75 - 19998.50) / 0.25 = 13 ticks
        self.assertEqual(ticks_tp, -20)        # -20 ticks por +20% de volumen
        self.assertEqual(precio_tp, 19993.50)

    def test_tp_techo_60_porciento_banda(self):
        """Si el volumen es +90% pero la distancia al 60% de la banda es de 24 ticks, el TP debe limitarse a 24."""
        # Banda a solo 10 puntos del VWAP: 60% de 10 puntos = 6.0 puntos = 24 ticks
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20000.0, 19998.0, 20010.0, 19990.0, "NQ", 20000.0, rvol=1.90 # +90% volumen
        )
        self.assertEqual(ticks_tp, 24) # Limitado al techo del 60% de la banda
        self.assertEqual(precio_tp, 20006.0)

    def test_tp_piso_minimo_5_ticks(self):
        """Si el volumen supera la media en solo un +2%, el TP debe respetar el piso de 5 ticks."""
        ticks_sl, ticks_tp, precio_sl, precio_tp = self.engine.calcular_riesgo_dinamico(
            "LONG", 20000.0, 19998.0, 20040.0, 19960.0, "NQ", 20000.0, rvol=1.02 # +2% volumen
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

    def test_break_even_activacion_al_50_porciento(self):
        """Verifica que al avanzar el 50% hacia el TP, el SL se mueve a Entrada + 1 tick en TopstepX."""
        # Configurar posición activa simulada en NQ
        self.engine.symbol_states["NQ"]["has_open_position"] = True
        self.mock_client.get_open_positions.return_value = [{"contractId": "CON_NQ_TEST", "size": 1}]
        self.mock_client.search_open_orders.return_value = [{"id": 9999, "contractId": "CON_NQ_TEST", "type": 4, "size": 1}]
        self.mock_client.modify_order.return_value = {"success": True}

        entry_p = 20000.0
        tp_p = 20020.0 # 20 puntos = 80 ticks
        halfway = 20010.0 # 50% de ganancia = 20010.0
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
            "be_triggered": False,
            "halfway_target": halfway,
            "be_price": be_p,
            "entry_timestamp": 100.0,
            "last_check_time": 0.0
        }

        # Simular precio de mercado que alcanzó el 50% (20010.50)
        df_update = pd.DataFrame([{'Close': 20010.50}])
        self.engine.monitorear_posicion_abierta("NQ", df_update)

        trade = self.engine.symbol_states["NQ"]["active_trade"]
        self.assertTrue(trade["be_triggered"], "Break-Even debió activarse al 50% de avance")
        self.assertEqual(trade["current_sl_price"], be_p, "El SL debió actualizarse a Entrada + 1 tick")
        self.mock_client.modify_order.assert_called_once_with(
            account_id=12345,
            order_id=9999,
            price=be_p,
            trigger_price=be_p,
            size=1
        )

    def test_independencia_entre_simbolos(self):
        """Verifica que un trade abierto en NQ no interfiere ni contamina el estado de ES."""
        self.engine.symbol_states["NQ"]["active_trade"] = {"symbol": "NQ", "direction": "LONG"}
        self.engine.symbol_states["NQ"]["has_open_position"] = True

        self.assertIsNone(self.engine.symbol_states["ES"]["active_trade"])
        self.assertFalse(self.engine.symbol_states["ES"]["has_open_position"])

if __name__ == '__main__':
    unittest.main()
