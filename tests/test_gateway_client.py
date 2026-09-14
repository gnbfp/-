"""``FeishuClient.send_image`` 的单测：真机 code=234011 的根因是「传了裸 bytes」。

``image.create`` 三组实测对照（我直接调飞书接口）：

  ==================================  ==============================
  ``image(bytes)``                    success=False code=234011
  ``image(BytesIO)``（带 ``.name``）  success=True  code=0
  ``image(open(path, "rb"))``         success=True  code=0
  ==================================  ==============================

所以这条测试盯的是**类型**：传给 SDK 的必须是带 ``read()`` 的文件对象，而且
``image.create`` 要在 ``with`` 块内部调用（文件得一直开着）。PNG 本身没问题。
"""

import pytest

from src.gateway.client import FeishuClient, FeishuError


class _Response:
    def __init__(self, ok=True, code=0, msg="", data=None) -> None:
        self._ok, self.code, self.msg, self.data = ok, code, msg, data

    def success(self) -> bool:
        return self._ok


class _ImageData:
    image_key = "img_key_from_upload"


class _Call:
    """假的 ``im.v1.<x>``：记下 request，回一个预置 response。

    调用当时就把 ``request_body.image.closed`` 记下来 —— ``with`` 块一退出文件就关了，
    事后再看只能看到 ``True``，测不出「上传时文件还开着」。
    """

    def __init__(self, sink, response) -> None:
        self._sink, self._response = sink, response
        self.closed_at_call: list = []

    def create(self, request):
        self._sink.append(request)
        image = getattr(getattr(request, "request_body", None), "image", None)
        if image is not None:
            self.closed_at_call.append(getattr(image, "closed", None))
        return self._response


class _V1:
    def __init__(self, uploads, sends) -> None:
        self.image = _Call(uploads, _Response(data=_ImageData()))
        self.message = _Call(sends, _Response())


class _Im:
    def __init__(self, v1) -> None:
        self.v1 = v1


class _FakeApi:
    def __init__(self, uploads, sends) -> None:
        self.im = _Im(_V1(uploads, sends))


def _client(upload=None):
    """把 ``_api_client`` 换成假的，绕开真实 SDK 网络调用。"""
    client = FeishuClient(config=None)
    uploads, sends = [], []
    api = _FakeApi(uploads, sends)
    if upload is not None:
        api.im.v1.image = _Call(uploads, upload)
    client._api_client = api
    return client, uploads, sends


def test_send_image_uploads_a_file_object_not_raw_bytes(tmp_path):
    """回归点：``image(bytes)`` 会被飞书拒（code=234011）；必须是文件对象。"""
    png = tmp_path / "gantt.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    client, uploads, sends = _client()

    assert client.send_image("c1", png) is True

    body = uploads[0].request_body
    assert not isinstance(body.image, (bytes, bytearray))    # 别传裸 bytes
    assert hasattr(body.image, "read")                       # 是文件对象
    assert client._api_client.im.v1.image.closed_at_call == [False]   # 上传时还开着
    assert sends[0].request_body.msg_type == "image"


def test_send_image_raises_when_the_upload_is_rejected(tmp_path):
    """上传被拒要抛 ``FeishuError``，并把 code/msg 带出来（别再只看到类名）。"""
    png = tmp_path / "gantt.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    client, _, _ = _client(
        upload=_Response(ok=False, code=234011, msg="Can't recognize image format.")
    )

    with pytest.raises(FeishuError) as info:
        client.send_image("c1", png)

    assert "234011" in str(info.value)
    assert "recognize" in str(info.value)
