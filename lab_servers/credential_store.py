"""Store remembered login secrets in the current user's native credential store."""
import ctypes
import sys
from contextlib import contextmanager


class CredentialError(ValueError):
    pass


class CredentialStore:
    def __init__(self):
        self._backend = None

    @property
    def name(self):
        return 'macOS 钥匙串' if sys.platform == 'darwin' else 'Windows 凭据管理器' if sys.platform == 'win32' else '系统凭据存储'

    def backend(self):
        if self._backend is None:
            if sys.platform == 'darwin':
                self._backend = MacKeychain()
            elif sys.platform == 'win32':
                self._backend = WindowsCredentials()
            else:
                raise CredentialError('此系统暂不支持记住密码，请取消该选项后连接。')
        return self._backend

    def get(self, key):
        return self.backend().get(key)

    def set(self, key, value):
        self.backend().set(key, value)

    def delete(self, key):
        self.backend().delete(key)


class MacKeychain:
    def __init__(self):
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        pointer, size = ctypes.c_void_p, ctypes.c_long
        for name, args, result in [
            ('CFStringCreateWithBytes', [pointer, pointer, size, ctypes.c_uint32, ctypes.c_bool], pointer),
            ('CFDataCreate', [pointer, pointer, size], pointer),
            ('CFDictionaryCreate', [pointer, pointer, pointer, size, pointer, pointer], pointer),
            ('CFDataGetLength', [pointer], size),
            ('CFDataGetBytePtr', [pointer], pointer),
            ('CFRelease', [pointer], None),
        ]:
            function = getattr(self.cf, name)
            function.argtypes, function.restype = args, result
        for name, args in [('SecItemAdd', [pointer, pointer]),
                           ('SecItemUpdate', [pointer, pointer]),
                           ('SecItemCopyMatching', [pointer, ctypes.POINTER(pointer)]),
                           ('SecItemDelete', [pointer])]:
            function = getattr(self.sec, name)
            function.argtypes, function.restype = args, ctypes.c_int32
        self.key_callbacks = ctypes.c_void_p.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks')
        self.value_callbacks = ctypes.c_void_p.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks')

    def constant(self, name):
        library = self.cf if name.startswith('kCF') else self.sec
        return ctypes.c_void_p.in_dll(library, name).value

    @contextmanager
    def dictionary(self, entries):
        owned, keys, values = [], [], []
        result = None
        try:
            for name, value in entries.items():
                keys.append(self.constant(name))
                if isinstance(value, str):
                    encoded = value.encode('utf-8')
                    value = self.cf.CFStringCreateWithBytes(None, encoded, len(encoded), 0x08000100, False)
                    owned.append(value)
                elif isinstance(value, bytes):
                    value = self.cf.CFDataCreate(None, value, len(value))
                    owned.append(value)
                values.append(value)
            array = ctypes.c_void_p * len(keys)
            result = self.cf.CFDictionaryCreate(None, array(*keys), array(*values), len(keys),
                                               ctypes.byref(self.key_callbacks), ctypes.byref(self.value_callbacks))
            if not result:
                raise CredentialError('无法创建钥匙串记录。')
            yield result
        finally:
            if result:
                self.cf.CFRelease(result)
            for value in owned:
                if value:
                    self.cf.CFRelease(value)

    def query(self, key):
        return {'kSecClass': self.constant('kSecClassGenericPassword'),
                'kSecAttrService': 'LabMonitor', 'kSecAttrAccount': 'server-' + key,
                'kSecUseAuthenticationUI': self.constant('kSecUseAuthenticationUIFail')}

    @staticmethod
    def check(status):
        if status == -25308:
            raise CredentialError('无法访问登录钥匙串，请解锁钥匙串后重试。')
        if status:
            raise CredentialError('钥匙串操作失败（{}）。'.format(status))

    def get(self, key):
        query = self.query(key)
        query['kSecReturnData'] = self.constant('kCFBooleanTrue')
        result = ctypes.c_void_p()
        with self.dictionary(query) as native:
            status = self.sec.SecItemCopyMatching(native, ctypes.byref(result))
        if status == -25300:
            return None
        self.check(status)
        try:
            return ctypes.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result)).decode('utf-8')
        finally:
            if result:
                self.cf.CFRelease(result)

    def set(self, key, value):
        query = self.query(key)
        data = {'kSecValueData': value.encode('utf-8')}
        with self.dictionary(query) as native, self.dictionary(data) as attributes:
            status = self.sec.SecItemUpdate(native, attributes)
        if status == -25300:
            with self.dictionary(dict(query, **data, kSecAttrLabel='服务器监控')) as attributes:
                status = self.sec.SecItemAdd(attributes, None)
        self.check(status)

    def delete(self, key):
        with self.dictionary(self.query(key)) as native:
            status = self.sec.SecItemDelete(native)
        if status != -25300:
            self.check(status)


class FileTime(ctypes.Structure):
    _fields_ = [('low', ctypes.c_uint32), ('high', ctypes.c_uint32)]


class WindowsCredential(ctypes.Structure):
    _fields_ = [('Flags', ctypes.c_uint32), ('Type', ctypes.c_uint32),
                ('TargetName', ctypes.c_wchar_p), ('Comment', ctypes.c_wchar_p),
                ('LastWritten', FileTime), ('CredentialBlobSize', ctypes.c_uint32),
                ('CredentialBlob', ctypes.c_void_p), ('Persist', ctypes.c_uint32),
                ('AttributeCount', ctypes.c_uint32), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', ctypes.c_wchar_p), ('UserName', ctypes.c_wchar_p)]


class WindowsCredentials:
    def __init__(self):
        self.api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        pointer = ctypes.POINTER(WindowsCredential)
        for name, args, result in [
            ('CredWriteW', [pointer, ctypes.c_uint32], ctypes.c_int),
            ('CredReadW', [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(pointer)], ctypes.c_int),
            ('CredDeleteW', [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
            ('CredFree', [ctypes.c_void_p], None),
        ]:
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def target(key):
        return 'LabMonitor/server-' + key

    @staticmethod
    def check(result):
        if not result:
            raise CredentialError('Windows 凭据操作失败（{}）。'.format(ctypes.get_last_error()))

    def get(self, key):
        result = ctypes.POINTER(WindowsCredential)()
        success = self.api.CredReadW(self.target(key), 1, 0, ctypes.byref(result))
        if not success and ctypes.get_last_error() == 1168:
            return None
        self.check(success)
        try:
            entry = result.contents
            return ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode('utf-8')
        finally:
            self.api.CredFree(result)

    def set(self, key, value):
        encoded = value.encode('utf-8')
        if len(encoded) > 2560:
            raise CredentialError('密码记录超过 Windows 凭据存储的长度限制。')
        buffer = ctypes.create_string_buffer(encoded)
        entry = WindowsCredential(Type=1, TargetName=self.target(key), UserName='LabMonitor',
                                  CredentialBlobSize=len(encoded), CredentialBlob=ctypes.addressof(buffer), Persist=2)
        self.check(self.api.CredWriteW(ctypes.byref(entry), 0))

    def delete(self, key):
        success = self.api.CredDeleteW(self.target(key), 1, 0)
        if not success and ctypes.get_last_error() == 1168:
            return
        self.check(success)
