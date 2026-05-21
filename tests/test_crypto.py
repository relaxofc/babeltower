from babeltower.crypto import canonical_request_string, generate_keypair, sign, verify


def test_sign_verify_roundtrip():
    private_key, public_key = generate_keypair()
    message = b"babeltower"
    signature = sign(private_key, message)

    assert verify(public_key, message, signature)
    assert not verify(public_key, b"tampered", signature)


def test_canonical_request_string_get_example():
    canonical = canonical_request_string(
        "GET",
        "/v1/inbox",
        "2026-05-21T14:32:11Z",
        b"",
    )

    assert canonical == (
        b"GET\n"
        b"/v1/inbox\n"
        b"2026-05-21T14:32:11Z\n"
        b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_canonical_request_string_includes_query_and_body_hash():
    canonical = canonical_request_string(
        "post",
        "/v1/intents?dry_run=true",
        "2026-05-21T14:32:11Z",
        b'{"match_type":"x"}',
    )

    assert canonical == (
        b"POST\n"
        b"/v1/intents?dry_run=true\n"
        b"2026-05-21T14:32:11Z\n"
        b"90f6c1b70f064b3f1950604eda173e14b66ac9f4b5909af5abf6f7a377b8608e"
    )
