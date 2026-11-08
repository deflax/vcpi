"""Public show grammar boundaries shared by terminal and daemon clients."""

import unittest

from vcpi.presentation.show_request import parse_show


class ShowRequestTests(unittest.TestCase):
    def test_details_preserves_target_and_can_precede_it(self):
        for arguments in ('controls "beatstep" --details', '--details controls beatstep'):
            request = parse_show(arguments)
            self.assertEqual(request.view, 'controlsbeatstep')
            self.assertTrue(request.details)
        effect = parse_show('1 fx "2" --details')
        self.assertEqual(effect.view, 'track1fx2')
        self.assertTrue(effect.details)

    def test_conflicting_targets_and_duplicate_flags_are_rejected(self):
        for arguments in ('--details --details', 'browse', 'browse --details', 'browse 1', 'browse midi',
                          'controls unknown', 'controls midimix beatstep',
                          '1 fx --details', '1 --unknown', '"midi', 'midi audio'):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                parse_show(arguments)


if __name__ == '__main__':
    unittest.main()
