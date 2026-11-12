import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path


TEMP_STORAGE = tempfile.TemporaryDirectory(prefix="cinelanka-tests-")
os.environ["CINELANKA_ADMIN_PASSWORD"] = "test-password-long-enough"
os.environ["CINELANKA_DATA_DIR"] = str(Path(TEMP_STORAGE.name) / "data")
os.environ["CINELANKA_MEDIA_DIR"] = str(Path(TEMP_STORAGE.name) / "uploads")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class WebsiteServerTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        TEMP_STORAGE.cleanup()

    def setUp(self):
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.headers, response.read()
        connection.close()
        return result

    def json_request(self, method, path, value=None, cookie=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        body = json.dumps(value).encode("utf-8") if value is not None else None
        status, response_headers, response_body = self.request(method, path, body, headers)
        return status, response_headers, json.loads(response_body) if response_body else None

    def upload(self, path, content, filename, content_type, cookie):
        return self.request(
            "PUT",
            path,
            content,
            {
                "Origin": self.origin,
                "Cookie": cookie,
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                "X-File-Name": filename,
            },
        )

    def create_uploaded_title(self, cookie, title, access, price=0):
        status, _, item = self.json_request(
            "POST",
            "/api/titles",
            {
                "title": title,
                "type": "Movie",
                "genre": "Drama",
                "description": "Test upload",
                "access": access,
                "price": price,
                "videoName": f"{title}.mp4",
                "coverName": f"{title}.png",
                "rights": True,
            },
            cookie,
            self.origin,
        )
        self.assertEqual(status, 201)
        identifier = item["id"]
        status, _, _ = self.upload(f"/api/titles/{identifier}/cover", b"test-poster", f"{title}.png", "image/png", cookie)
        self.assertEqual(status, 200)
        status, _, _ = self.upload(f"/api/titles/{identifier}/video", b"movie-file-bytes", f"{title}.mp4", "video/mp4", cookie)
        self.assertEqual(status, 200)
        return identifier

    def test_owner_upload_public_catalog_paid_access_and_receipts(self):
        status, _, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"CineLanka VIP", page)

        status, session_headers, session = self.json_request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(session["isAdmin"])
        visitor_cookie = session_headers.get("Set-Cookie").split(";", 1)[0]

        status, _, catalog = self.json_request("GET", "/api/titles")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["titles"], [])

        status, _, _ = self.json_request(
            "POST", "/api/titles", {"title": "Intruder", "rights": True}, origin=self.origin
        )
        self.assertEqual(status, 401)

        status, _, _ = self.json_request(
            "POST", "/api/titles", {"title": "Intruder"}, origin="http://attacker.example"
        )
        self.assertEqual(status, 403)

        status, _, _ = self.json_request(
            "POST", "/api/session", {"password": "wrong-password"}, origin=self.origin
        )
        self.assertEqual(status, 401)
        status, session_headers, session = self.json_request(
            "POST",
            "/api/session",
            {"password": os.environ["CINELANKA_ADMIN_PASSWORD"]},
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        self.assertTrue(session["isAdmin"])
        admin_cookie = session_headers.get("Set-Cookie").split(";", 1)[0]

        free_id = self.create_uploaded_title(admin_cookie, "Free Movie", "free")
        status, _, catalog = self.json_request("GET", "/api/titles", cookie=visitor_cookie)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in catalog["titles"]], [free_id])
        self.assertEqual(self.request("GET", f"/media/{free_id}/video", headers={"Cookie": visitor_cookie})[2], b"movie-file-bytes")
        status, response_headers, video_range = self.request(
            "GET",
            f"/media/{free_id}/video",
            headers={"Cookie": visitor_cookie, "Range": "bytes=0-4"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(response_headers.get("Content-Range"), "bytes 0-4/16")
        self.assertEqual(video_range, b"movie")
        status, _, _ = self.json_request(
            "DELETE", "/api/session", cookie=admin_cookie, origin=self.origin
        )
        self.assertEqual(status, 204)
        status, _, _ = self.json_request(
            "POST", "/api/titles", {"title": "Not the owner"}, cookie=admin_cookie, origin=self.origin
        )
        self.assertEqual(status, 401)

        status, session_headers, _ = self.json_request(
            "POST",
            "/api/session",
            {"password": os.environ["CINELANKA_ADMIN_PASSWORD"]},
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        admin_cookie = session_headers.get("Set-Cookie").split(";", 1)[0]
        paid_id = self.create_uploaded_title(admin_cookie, "Paid Movie", "paid", 500)

        status, _, _ = self.request("GET", f"/media/{paid_id}/video", headers={"Cookie": visitor_cookie})
        self.assertEqual(status, 403)

        status, _, payment = self.json_request(
            "POST",
            "/api/payment-requests",
            {"titleId": paid_id},
            cookie=visitor_cookie,
            origin=self.origin,
        )
        self.assertEqual(status, 201)
        payment_id = payment["id"]
        status, _, _ = self.upload(
            f"/api/payment-requests/{payment_id}/receipt",
            b"receipt-image",
            "receipt.png",
            "image/png",
            visitor_cookie,
        )
        self.assertEqual(status, 200)

        status, _, queue = self.json_request("GET", "/api/admin/payments", cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(queue["payments"]), 1)
        status, _, _ = self.json_request("GET", "/api/admin/payments", cookie=visitor_cookie)
        self.assertEqual(status, 401)
        status, _, receipt = self.request(
            "GET", f"/api/admin/payments/{payment_id}/receipt", headers={"Cookie": admin_cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(receipt, b"receipt-image")

        status, _, _ = self.json_request(
            "POST",
            f"/api/admin/payments/{payment_id}/approve",
            {},
            cookie=admin_cookie,
            origin=self.origin,
        )
        self.assertEqual(status, 200)
        status, _, catalog = self.json_request("GET", "/api/titles", cookie=visitor_cookie)
        self.assertTrue(next(item for item in catalog["titles"] if item["id"] == paid_id)["unlocked"])
        status, _, video = self.request(
            "GET", f"/media/{paid_id}/video", headers={"Cookie": visitor_cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(video, b"movie-file-bytes")

        status, _, catalog = self.json_request("GET", "/api/titles", cookie=visitor_cookie)
        self.assertEqual({item["id"] for item in catalog["titles"]}, {free_id, paid_id})


if __name__ == "__main__":
    unittest.main()
