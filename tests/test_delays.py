from utils.delays import random_delay


def test_random_delay_in_range():
    for _ in range(100):
        d = random_delay(1000, 2000)
        assert 1.0 <= d <= 2.0


def test_random_delay_returns_float():
    d = random_delay(500, 1500)
    assert isinstance(d, float)
