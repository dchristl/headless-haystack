#!/usr/bin/env python
import os
import glob
import datetime
import time
import getpass
import base64
import json
import hashlib
import hmac
import codecs
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.backends import default_backend
import objc
from Foundation import NSBundle, NSClassFromString, NSData, NSPropertyListSerialization
import urllib
import six
import ssl
import sys

pwd = 'alpine'  # Keychain password, can be hardcoded


def bytes_to_int(b):
    return int(codecs.encode(b, 'hex'), 16)


def sha256(data):
    digest = hashlib.new("sha256")
    digest.update(data)
    return digest.digest()


def decrypt(enc_data, algorithm_dkey, mode):
    decryptor = Cipher(algorithm_dkey, mode, default_backend()).decryptor()
    return decryptor.update(enc_data) + decryptor.finalize()


def unpad(paddedBinary, blocksize):
    unpadder = PKCS7(blocksize).unpadder()
    return unpadder.update(paddedBinary) + unpadder.finalize()


def readKeychain():
    # https://github.com/libyal/dtformats/blob/main/documentation/MacOS%20keychain%20database%20file%20format.asciidoc
    res = [None] * 7
    with open("%s/Library/Keychains/login.keychain-db" % os.path.expanduser("~"), 'rb') as db:
        kc = db.read()

        def get_table_offsets(tbl_array_offset):
            ntables = bytes_to_int(
                kc[tbl_array_offset + 4: tbl_array_offset + 8])
            tbl_offsets_b = kc[tbl_array_offset +
                               8: tbl_array_offset + 8 + (ntables * 4)]
            return [bytes_to_int(tbl_offsets_b[i:i+4]) + tbl_array_offset for i in six.moves.range(0, len(tbl_offsets_b), 4)]

        def get_record_offsets(tbl_start):
            nrecords = bytes_to_int(kc[tbl_start + 24: tbl_start + 28])
            rec_offsets_b = kc[tbl_start + 28: tbl_start + 28 + (nrecords * 4)]
            rec_offsets = [bytes_to_int(
                rec_offsets_b[i:i+4]) + tbl_start for i in six.moves.range(0, len(rec_offsets_b), 4)]
            # remove 0 offset records and empty records
            return [ro for ro in rec_offsets if ro != tbl_start and bytes_to_int(kc[ro: ro + 4])]

        def match_record_attribute(rec_start, rec_nattr, rec_attr, attr_match):
            attr_offsets_b = kc[rec_start +
                                24: rec_start + 24 + (rec_nattr * 4)]
            attr_offsets = [bytes_to_int(
                attr_offsets_b[i:i+4]) + rec_start - 1 for i in six.moves.range(0, len(attr_offsets_b), 4)]
            # non-zero offset, and no weird big values
            if attr_offsets[0] and attr_offsets[0] < rec_start + bytes_to_int(kc[rec_start: rec_start + 4]):
                if kc[attr_offsets[rec_attr] + 4: attr_offsets[rec_attr] + 4 + bytes_to_int(kc[attr_offsets[rec_attr]: attr_offsets[rec_attr] + 4])] == attr_match:
                    # return record blob data (NOTE not sure about BLOB size!!!)
                    return kc[rec_start + 24 + (rec_nattr * 4): rec_start + 24 + (rec_nattr * 4) + bytes_to_int(kc[rec_start + 16: rec_start + 20])]
            return None

        if kc[:4] == b'kych':
            tbl_offsets = get_table_offsets(bytes_to_int(kc[12:16]))
            symmetric_key_idx = None
            # walk backwards so we get the generic password blob before the symmetric key, we need that to select which key to take
            for tbl_start in tbl_offsets[::-1]:
                if kc[tbl_start + 4: tbl_start + 8] == b'\x00\x00\x00\x11':  # Symmetric key
                    rec_offsets = get_record_offsets(tbl_start)
                    for rec_start in rec_offsets:
                        # might be wrong about amount of attributes
                        symmetric_key_blob = match_record_attribute(
                            rec_start, 27, 1, symmetric_key_idx)
                        if symmetric_key_blob:
                            start_crypto_blob = bytes_to_int(
                                symmetric_key_blob[8:12])
                            total_length = bytes_to_int(
                                symmetric_key_blob[12:16])
                            res[2] = symmetric_key_blob[16:24]
                            res[3] = symmetric_key_blob[24 +
                                                        (start_crypto_blob - 0x18): 24 + (total_length - 0x18)]
                            break
                elif kc[tbl_start + 4: tbl_start + 8] == b'\x80\x00\x00\x00':  # Generic passwords
                    rec_offsets = get_record_offsets(tbl_start)
                    for rec_start in rec_offsets:
                        # generic password record has 16 attributes
                        icloud_key_blob = match_record_attribute(
                            rec_start, 16, 14, b'iCloud')
                        if icloud_key_blob:
                            symmetric_key_idx = icloud_key_blob[:20]
                            res[0] = icloud_key_blob[20:28]
                            res[1] = icloud_key_blob[28:]
                            break
                # Metadata, containing master key and db key
                elif kc[tbl_start + 4: tbl_start + 8] == b'\x80\x00\x80\x00':
                    rec_start = get_record_offsets(tbl_start)[0]
                    db_key_blob = kc[rec_start + 24: rec_start + 24 + bytes_to_int(
                        kc[rec_start + 16: rec_start + 20])]  # 2nd record is the one we want
                    res[4] = db_key_blob[44:64]
                    res[5] = db_key_blob[64:72]
                    res[6] = db_key_blob[120:168]
    return res


def retrieveICloudKey():
    icloud_key_IV, icloud_key_enc, symmetric_key_IV, symmetric_key_enc, db_key_salt, db_key_IV, db_key_enc = readKeychain()
    master_key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=24, salt=db_key_salt,
                            iterations=1000, backend=default_backend()).derive(bytes(pwd))
    db_key = unpad(decrypt(db_key_enc, algorithms.TripleDES(
        master_key), modes.CBC(db_key_IV)), algorithms.TripleDES.block_size)[:24]
    p1 = unpad(decrypt(symmetric_key_enc, algorithms.TripleDES(db_key), modes.CBC(
        b'J\xdd\xa2,y\xe8!\x05')), algorithms.TripleDES.block_size)
    symmetric_key = unpad(decrypt(p1[:32][::-1], algorithms.TripleDES(
        db_key), modes.CBC(symmetric_key_IV)), algorithms.TripleDES.block_size)[4:]
    icloud_key = unpad(decrypt(icloud_key_enc, algorithms.TripleDES(
        symmetric_key), modes.CBC(icloud_key_IV)), algorithms.TripleDES.block_size)
    return icloud_key


def getAppleDSIDandSearchPartyToken(iCloudKey):
    # copied from https://github.com/Hsn723/MMeTokenDecrypt
    decryption_key = hmac.new(b't9s\"lx^awe.580Gj%\'ld+0LG<#9xa?>vb)-fkwb92[}', base64.b64decode(
        iCloudKey), digestmod=hashlib.md5).digest()
    mmeTokenFile = glob.glob(
        "%s/Library/Application Support/iCloud/Accounts/[0-9]*" % os.path.expanduser("~"))[0]
    decryptedBinary = unpad(decrypt(open(mmeTokenFile, 'rb').read(), algorithms.AES(
        decryption_key), modes.CBC(b'\00' * 16)), algorithms.AES.block_size)
    binToPlist = NSData.dataWithBytes_length_(
        decryptedBinary, len(decryptedBinary))
    tokenPlist = NSPropertyListSerialization.propertyListWithData_options_format_error_(
        binToPlist, 0, None, None)[0]
    return tokenPlist["appleAccountInfo"]["dsPrsID"], tokenPlist["tokens"]['searchPartyToken']


def getOTPHeaders():
    AOSKitBundle = NSBundle.bundleWithPath_(
        '/System/Library/PrivateFrameworks/AOSKit.framework')
    objc.loadBundleFunctions(AOSKitBundle, globals(), [
                             ("retrieveOTPHeadersForDSID", b'')])
    util = NSClassFromString('AOSUtilities')
    anisette = str(util.retrieveOTPHeadersForDSID_(
        "-2")).replace('"', ' ').replace(';', ' ').split()
    return anisette[6], anisette[3]


def getCurrentTimes():
    clientTime = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
    clientTimestamp = int(datetime.datetime.now().strftime('%s'))
    return clientTime, time.tzname[1], clientTimestamp


class ServerHandler(six.moves.SimpleHTTPServer.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200, "ok")
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header("Access-Control-Allow-Headers", "X-Requested-With")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.getheader('content-length', 0))

        post_body = self.rfile.read(content_len)

        print('Getting with post: ' + post_body)
        UTCTime, Timezone, unixEpoch = getCurrentTimes()
        body = json.loads(post_body)
        startdate = unixEpoch - 60 * 60 * 24 * 7 
        data = '{"search": [{"endDate": %d, "startDate": %d, "ids": [\'%s\']}]}' % ((unixEpoch -978307200) *1000000, (startdate -978307200)*1000000, "','".join(body['ids']))

        print(data)
        iCloud_decryptionkey = retrieveICloudKey()
        AppleDSID, searchPartyToken = getAppleDSIDandSearchPartyToken(
            iCloud_decryptionkey)
        machineID, oneTimePassword = getOTPHeaders()
        UTCTime, Timezone, unixEpoch = getCurrentTimes()

        request_headers = {
            'Authorization': "Basic %s" % (base64.b64encode((AppleDSID + ':' + searchPartyToken).encode('ascii')).decode('ascii')),
            'X-Apple-I-MD': "%s" % (oneTimePassword),
            'X-Apple-I-MD-RINFO': '17106176',
            'X-Apple-I-MD-M': "%s" % (machineID),
            'X-Apple-I-TimeZone': "%s" % (Timezone),
            'X-Apple-I-Client-Time': "%s" % (UTCTime),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-BA-CLIENT-TIMESTAMP': "%s" % (unixEpoch)
        }

        conn = six.moves.http_client.HTTPSConnection(
            'gateway.icloud.com', timeout=5, context=ssl._create_unverified_context())
        conn.request("POST", "/acsnservice/fetch", data, request_headers)
        res = conn.getresponse()
        # self.wfile.write(res.read())

        self.send_response(200)
        # send response headers
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        # send the body of the response
        responseBody = res.read()
        self.wfile.write(responseBody)

        print(responseBody)


PORT = 6176
if __name__ == "__main__":
    if not pwd:
        pwd = getpass.getpass('Keychain password:')
    isV3 = sys.version_info.major > 2
    print('Using python3' if isV3 else 'Using python2')
    Handler = ServerHandler

    httpd = six.moves.socketserver.TCPServer(("", PORT), Handler)

    print("serving at port " + str(PORT))

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print('Server stopped')
