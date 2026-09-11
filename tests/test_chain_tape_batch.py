from app.chain.tape import AdaptiveBatchSize, is_batch_too_large_error


def test_shrink_halves_down_to_the_floor():
    batch = AdaptiveBatchSize(current=2000, minimum=50, maximum=2000)

    sizes = [batch.shrink() for _ in range(7)]

    assert sizes == [1000, 500, 250, 125, 62, 50, 50]


def test_growth_needs_three_consecutive_clean_passes():
    batch = AdaptiveBatchSize(current=250, minimum=50, maximum=2000)

    batch.record_success()
    batch.record_success()
    assert batch.current == 250  # only two clean passes so far

    batch.record_success()
    assert batch.current == 500  # third pass doubles it


def test_growth_is_capped_at_the_configured_maximum():
    batch = AdaptiveBatchSize(current=1500, minimum=50, maximum=2000)

    for _ in range(6):
        batch.record_success()

    assert batch.current == 2000


def test_shrink_resets_the_clean_pass_counter():
    batch = AdaptiveBatchSize(current=1000, minimum=50, maximum=2000)
    batch.record_success()
    batch.record_success()  # two clean passes, about to earn a third...

    batch.shrink()  # ...but a shrink to 500 must void that progress
    batch.record_success()
    batch.record_success()
    assert batch.current == 500  # still needs one more clean pass post-shrink

    batch.record_success()
    assert batch.current == 1000


def test_batch_too_large_error_matches_known_provider_phrasings():
    assert is_batch_too_large_error(Exception("query returned more than 10000 results"))
    assert is_batch_too_large_error(Exception("requested block range is too wide"))
    assert is_batch_too_large_error(Exception("Error: -32005 limit exceeded"))


def test_batch_too_large_error_does_not_match_unrelated_failures():
    assert not is_batch_too_large_error(Exception("connection refused"))
    assert not is_batch_too_large_error(Exception("timeout"))
