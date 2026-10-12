import unittest
from backup_controller.main import validate, desired_cronjob

class OperatorTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate({'schedule': '0 * * * *', 'target': {'kind': 'PersistentVolumeClaim', 'name': 'data'}, 'retention': 3})[2], 3)

    def test_invalid_cron(self):
        with self.assertRaises(ValueError):
            validate({'schedule': 'oops', 'target': {'kind': 'PersistentVolumeClaim', 'name': 'data'}})

    def test_negative_retention(self):
        with self.assertRaises(ValueError):
            validate({'schedule': '0 * * * *', 'target': {'kind': 'PersistentVolumeClaim', 'name': 'data'}, 'retention': -1})

    def test_owner_and_suspend(self):
        job = desired_cronjob('backup', 'default', 'uid-123', '0 * * * *', 'data', True)
        self.assertEqual(job['metadata']['ownerReferences'][0]['uid'], 'uid-123')
        self.assertTrue(job['spec']['suspend'])

if __name__ == '__main__':
    unittest.main()
