import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from publish_six_mapping_corrections_20261015 import rollback_action

class ConcurrentReleaseUpdates(unittest.TestCase):
    def test_third_party_newer_edit_is_preserved(self):
        self.assertEqual(rollback_action('user-newer','original','ours'),'preserve_newer')

    def test_only_our_own_replacement_can_be_clobbered(self):
        self.assertEqual(rollback_action('ours','original','ours'),'restore_ours')
        self.assertEqual(rollback_action('original','original','ours'),'keep')

    def test_missing_asset_uses_nonclobber_restore(self):
        self.assertEqual(rollback_action(None,'original','ours'),'restore_missing')

if __name__=='__main__':unittest.main()
