"""Routing eligibility and controller destinations from cached state only."""

from types import SimpleNamespace
import unittest

from core.audio_state import ParameterSnapshot, PluginSnapshot
from core.models import InstrumentSlot
from core.sequencer import SequenceBank
from graph.signal_flow import build_flow_snapshot


class FlowTests(unittest.TestCase):
    def setUp(self):
        instrument = PluginSnapshot(1, name="Synth", parameters={
            "cutoff": ParameterSnapshot(range=(20, 20000)),
            "enabled": ParameterSnapshot(kind="bool"),
            "mode": ParameterSnapshot(kind="enum", valid_values=("Soft", "Hard")),
            "unmapped": ParameterSnapshot(range=(0, 1)),
        }, values={"cutoff": 880.0, "enabled": True, "mode": "Hard"})
        self.slot = InstrumentSlot("Lead", "lead.vst3", instrument,
                                   effects=[PluginSnapshot(2, name="Delay", parameters={
                                       "effect_mix": ParameterSnapshot(range=(0, 1))})])
        self.engine = SimpleNamespace(
            slots=[self.slot] + [None] * 7, master_effects=[], master_gain=1.0,
            channel_map={0: 0, 15: 7}, _failure=None, _closed=False, _health=[1],
            audio_output_name="Output", backend_label="", sample_rate=48000,
            buffer_size=256, output_channels=2,
        )
        self.host = SimpleNamespace(
            engine=self.engine, channel_map=self.engine.channel_map,
            midi_inputs=[SimpleNamespace(port_name="Keyboard")],
            midimix=SimpleNamespace(input_port_name="Mixer", output_port_name=None),
            sequencer=SimpleNamespace(banks=[SequenceBank([60], linked_slot=7)] + [None]*15,
                                      _running=True),
            link=SimpleNamespace(enabled=False, _bpm=120),
        )

    def rows(self, section):
        return next(s['rows'] for s in build_flow_snapshot(self.host)['sections']
                    if s['id'] == section)

    def slot_state(self):
        return self.rows('slot1')[0][1]

    def test_output_eligibility_tracks_independent_blockers(self):
        self.assertIn('eligible', self.slot_state())
        for field, value, reason in (
            ('muted', True, 'muted'), ('enabled', False, 'disabled'),
            ('worker_failure', 'worker crashed', 'worker crashed'), ('gain', 0, 'gain zero'),
        ):
            original = getattr(self.slot, field)
            setattr(self.slot, field, value)
            self.assertIn(reason, self.slot_state())
            self.assertNotIn('eligible', self.slot_state())
            setattr(self.slot, field, original)
        self.engine.slots[1] = InstrumentSlot('Solo', '', PluginSnapshot(3), solo=True)
        self.assertIn('solo-excluded', self.slot_state())
        self.slot.solo = True
        self.assertIn('eligible', self.slot_state())
        self.engine._failure = 'owner crashed'
        self.assertIn('audio owner failed', self.slot_state())
        self.assertNotIn('eligible', self.slot_state())

    def test_knobs_target_instrument_parameters_not_inserts_or_extra_parameters(self):
        knobs = [row for row in self.rows('controls') if row[0].startswith('Slot 1 knob')]
        self.assertEqual([row[2].split(': ', 1)[1] for row in knobs],
                         ['cutoff', 'enabled', 'mode'])
        self.assertIn('880', knobs[0][3])
        self.assertIn('20 .. 20000', knobs[0][3])
        self.assertIn('true', knobs[1][3])
        self.assertIn('Soft, Hard', knobs[2][3])
        self.host.midimix.input_port_name = None
        self.assertTrue(all('inactive' in row[4] for row in self.rows('controls')
                            if row[0].startswith('Slot 1 knob')))

    def test_empty_destinations_remain_visible_for_midi_and_sequencer(self):
        rows = self.rows('midi')
        channel = next(row for row in rows if row[0] == 'MIDI channel 16')
        bank = next(row for row in rows if row[0] == 'Sequencer bank 1')
        self.assertIn('Slot 8', channel[1])
        self.assertIn('empty', channel[2])
        self.assertIn('Slot 8', bank[1])
        self.assertIn('empty', bank[2])
        self.assertIn('C4 (60)', bank[2])


if __name__ == '__main__':
    unittest.main()
