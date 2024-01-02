import unittest

from smart_espresso.smart_espresso import SmartEspresso


class TestSmartEspresso(unittest.TestCase):

    def test_smart_espresso(self):
        se = SmartEspresso([], None, None)
        self.assertRaises(ValueError, se.run)



if __name__ == '__main__':
    unittest.main()
