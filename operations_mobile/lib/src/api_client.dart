import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'config.dart';
import 'models.dart';

class ApiException implements Exception {
  const ApiException(this.message, {this.statusCode, this.data});
  final String message;
  final int? statusCode;
  final Map<String, dynamic>? data;
  @override
  String toString() => message;
}

String parseOperationsToken(Map<String, dynamic> data) {
  final token = data['token'];
  if (token is! String || token.trim().isEmpty) {
    throw const ApiException('استجابة تسجيل الدخول غير مكتملة. حاول مجدداً.');
  }
  return token.trim();
}

String? resolveOperationsToken({
  required String? storedToken,
  required String provisionedToken,
}) {
  final provisioned = provisionedToken.trim();
  if (provisioned.isNotEmpty) return provisioned;
  final stored = storedToken?.trim() ?? '';
  return stored.isEmpty ? null : stored;
}

class OperationsApi {
  OperationsApi()
    : _dio = Dio(
        BaseOptions(
          baseUrl: AppConfig.apiBaseUrl,
          connectTimeout: const Duration(seconds: 10),
          receiveTimeout: const Duration(seconds: 15),
          headers: const {'Accept': 'application/json'},
        ),
      ) {
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (_token != null && options.extra['skipAuth'] != true) {
            options.headers['Authorization'] = 'Ops-Token $_token';
          }
          handler.next(options);
        },
      ),
    );
  }

  static const _storage = FlutterSecureStorage(aOptions: AndroidOptions());
  static const _tokenKey = 'operations_access_token';
  final Dio _dio;
  String? _token;

  Future<bool> restoreSession() async {
    final stored = await _storage.read(key: _tokenKey);
    _token = resolveOperationsToken(
      storedToken: stored,
      provisionedToken: AppConfig.provisionedAccessToken,
    );
    if (_token != null && _token != stored) {
      await _storage.write(key: _tokenKey, value: _token);
    }
    return _token != null;
  }

  Future<void> login({
    required String phone,
    required String password,
    required String deviceName,
    String otp = '',
  }) async {
    final data = await _request(
      () => _dio.post<Map<String, dynamic>>(
        '/auth/login/',
        options: Options(extra: {'skipAuth': true}),
        data: {
          'phone': phone,
          'password': password,
          'device_name': deviceName,
          if (otp.isNotEmpty) 'otp': otp,
        },
      ),
    );
    _token = parseOperationsToken(data);
    await _storage.write(key: _tokenKey, value: _token);
  }

  Future<void> logout() async {
    try {
      await _request(() => _dio.post<Map<String, dynamic>>('/auth/logout/'));
    } catch (_) {
      // Local logout must still complete if the server is unavailable.
    } finally {
      _token = null;
      await _storage.delete(key: _tokenKey);
    }
  }

  Future<void> clearSession() async {
    _token = null;
    await _storage.delete(key: _tokenKey);
  }

  Future<DashboardData> dashboard() async => DashboardData.fromJson(
    await _request(() => _dio.get<Map<String, dynamic>>('/dashboard/')),
  );

  Future<ProjectDetails> project(int id) async => ProjectDetails.fromJson(
    await _request(() => _dio.get<Map<String, dynamic>>('/projects/$id/')),
  );

  Future<ProviderOverview> providerOverview(int serverId) async =>
      ProviderOverview.fromJson(
        await _request(
          () => _dio.get<Map<String, dynamic>>('/servers/$serverId/provider/'),
        ),
      );

  Future<ProviderActionInfo> providerAction(
    int serverId,
    String action,
    String confirmation,
  ) async => ProviderActionInfo.fromJson(
    await _request(
      () => _dio.post<Map<String, dynamic>>(
        '/servers/$serverId/provider/actions/',
        data: {'action': action, 'confirmation': confirmation},
      ),
    ),
  );

  Future<ProviderActionInfo> providerActionStatus(
    int serverId,
    int actionId,
  ) async => ProviderActionInfo.fromJson(
    await _request(
      () => _dio.get<Map<String, dynamic>>(
        '/servers/$serverId/provider/actions/$actionId/',
      ),
    ),
  );

  Future<DeploymentOverview> deploymentStatus() async =>
      DeploymentOverview.fromJson(
        await _request(
          () => _dio.get<Map<String, dynamic>>('/deployment/status/'),
        ),
      );

  Future<DeploymentInfo> triggerDeployment({
    required int projectId,
    required String confirmation,
  }) async {
    final data = await _request(
      () => _dio.post<Map<String, dynamic>>(
        '/deployment/deploy/',
        data: {'project_id': projectId, 'confirmation': confirmation},
      ),
    );
    return DeploymentInfo.fromJson(
      Map<String, dynamic>.from(data['state'] as Map? ?? data),
    );
  }

  Future<OperationActionInfo> runAction(
    int projectId,
    String action, {
    int? serviceId,
    String? confirmation,
    int? sinceMinutes,
    int? tail,
  }) async {
    return OperationActionInfo.fromJson(
      await _request(
        () => _dio.post<Map<String, dynamic>>(
          '/projects/$projectId/actions/',
          data: {
            'action': action,
            'service_id': ?serviceId,
            'confirmation': ?confirmation,
            'since_minutes': ?sinceMinutes,
            'tail': ?tail,
          },
        ),
      ),
    );
  }

  Future<OperationActionInfo> actionDetail(int projectId, int actionId) async =>
      OperationActionInfo.fromJson(
        await _request(
          () => _dio.get<Map<String, dynamic>>(
            '/projects/$projectId/actions/$actionId/',
          ),
        ),
      );

  Future<PaymentLinksData> paymentLinks() async => PaymentLinksData.fromJson(
    await _request(() => _dio.get<Map<String, dynamic>>('/payment-links/')),
  );

  Future<PaymentLinkInfo> createPaymentLink({
    required int projectId,
    required String customerName,
    required String customerPhone,
    required double amount,
    required String description,
    String customerEmail = '',
    String internalReference = '',
    DateTime? expiresAt,
  }) async {
    return PaymentLinkInfo.fromJson(
      await _request(
        () => _dio.post<Map<String, dynamic>>(
          '/payment-links/',
          data: {
            'project_id': projectId,
            'customer_name': customerName,
            'customer_phone': customerPhone,
            'customer_email': customerEmail,
            'amount': amount.toStringAsFixed(2),
            'description': description,
            'internal_reference': internalReference,
            if (expiresAt != null)
              'expires_at': expiresAt.toUtc().toIso8601String(),
          },
        ),
      ),
    );
  }

  Future<PaymentLinkInfo> syncPaymentLink(String publicId) async {
    return PaymentLinkInfo.fromJson(
      await _request(
        () => _dio.post<Map<String, dynamic>>('/payment-links/$publicId/sync/'),
      ),
    );
  }

  Future<PaymentLinkInfo> cancelPaymentLink(
    String publicId, {
    required String confirmation,
  }) async {
    return PaymentLinkInfo.fromJson(
      await _request(
        () => _dio.post<Map<String, dynamic>>(
          '/payment-links/$publicId/cancel/',
          data: {'confirmation': confirmation},
        ),
      ),
    );
  }

  Future<List<OperationsAccount>> accounts() async {
    final data = await _request(
      () => _dio.get<Map<String, dynamic>>('/accounts/'),
    );
    return (data['accounts'] as List? ?? const [])
        .map(
          (item) => OperationsAccount.fromJson(
            Map<String, dynamic>.from(item as Map),
          ),
        )
        .toList();
  }

  Future<OperationsAccount> createAccount({
    required String name,
    required String phone,
    required String password,
    String email = '',
    String role = 'viewer',
  }) async {
    return OperationsAccount.fromJson(
      await _request(
        () => _dio.post<Map<String, dynamic>>(
          '/accounts/',
          data: {
            'name': name,
            'phone': phone,
            'password': password,
            'role': role,
            if (email.isNotEmpty) 'email': email,
          },
        ),
      ),
    );
  }

  Future<OperationsAccount> updateAccount(
    int id, {
    String? name,
    String? email,
    String? password,
    bool? isActive,
    String? role,
  }) async {
    return OperationsAccount.fromJson(
      await _request(
        () => _dio.patch<Map<String, dynamic>>(
          '/accounts/$id/',
          data: {
            'name': ?name,
            'email': ?email,
            'password': ?password,
            'is_active': ?isActive,
            'role': ?role,
          },
        ),
      ),
    );
  }

  Future<void> changePassword({
    required String currentPassword,
    required String newPassword,
  }) async {
    await _request(
      () => _dio.post<Map<String, dynamic>>(
        '/auth/password/',
        data: {
          'current_password': currentPassword,
          'new_password': newPassword,
        },
      ),
    );
  }

  Future<void> acknowledgeIncident(int id) async {
    await _request(
      () => _dio.post<Map<String, dynamic>>('/incidents/$id/acknowledge/'),
    );
  }

  Future<void> registerDevice({
    required String deviceId,
    required String name,
    required String fcmToken,
  }) async {
    await _request(
      () => _dio.post<Map<String, dynamic>>(
        '/devices/',
        data: {
          'device_id': deviceId,
          'name': name,
          'platform': 'android',
          'fcm_token': fcmToken,
          'alerts_enabled': true,
        },
      ),
    );
  }

  Future<Map<String, dynamic>> _request(
    Future<Response<Map<String, dynamic>>> Function() call,
  ) async {
    try {
      final response = await call();
      return response.data ?? <String, dynamic>{};
    } on DioException catch (error) {
      final raw = error.response?.data;
      final data = raw is Map ? Map<String, dynamic>.from(raw) : null;
      final detail = data?['detail']?.toString();
      throw ApiException(
        detail?.isNotEmpty == true ? detail! : _networkMessage(error),
        statusCode: error.response?.statusCode,
        data: data,
      );
    }
  }

  String _networkMessage(DioException error) {
    if (error.type == DioExceptionType.connectionTimeout ||
        error.type == DioExceptionType.receiveTimeout) {
      return 'انتهت مهلة الاتصال بالخادم.';
    }
    if (error.type == DioExceptionType.connectionError) {
      return 'تعذر الاتصال. تحقق من الإنترنت وحالة الخادم.';
    }
    return 'تعذر إكمال الطلب الآن.';
  }
}

class EmergencyApi {
  EmergencyApi()
    : _dio = Dio(
        BaseOptions(
          baseUrl: AppConfig.emergencyUrl,
          connectTimeout: const Duration(seconds: 8),
          receiveTimeout: const Duration(seconds: 15),
        ),
      );

  static const _storage = FlutterSecureStorage(aOptions: AndroidOptions());
  static const _key = 'operations_emergency_access_token';
  final Dio _dio;

  Future<bool> hasToken() async =>
      (await _storage.read(key: _key))?.isNotEmpty == true;

  Future<void> saveToken(String token) async {
    final value = token.trim();
    if (value.length < 32) {
      throw const ApiException('مفتاح الطوارئ قصير أو غير صالح.');
    }
    await _storage.write(key: _key, value: value);
  }

  Future<void> clearToken() => _storage.delete(key: _key);

  Future<Map<String, dynamic>> status() =>
      _request(() => _dio.get<Map<String, dynamic>>('/v1/server'));

  Future<Map<String, dynamic>> overviewData() =>
      _request(() => _dio.get<Map<String, dynamic>>('/v1/overview'));

  Future<ProviderOverview> overview() async => ProviderOverview.fromJson({
    'configured': true,
    'can_control': false,
    'server': await _request(
      () => _dio.get<Map<String, dynamic>>('/v1/overview'),
    ),
  });

  Future<Map<String, dynamic>> action(String action, String confirmation) =>
      _request(
        () => _dio.post<Map<String, dynamic>>(
          '/v1/actions/$action',
          data: {'confirmation': confirmation},
        ),
      );

  Future<Map<String, dynamic>> actionStatus(int providerActionId) => _request(
    () => _dio.get<Map<String, dynamic>>('/v1/actions/$providerActionId'),
  );

  Future<Map<String, dynamic>> _request(
    Future<Response<Map<String, dynamic>>> Function() call,
  ) async {
    if (!AppConfig.emergencyUrl.startsWith('https://')) {
      throw const ApiException('مسار الطوارئ المستقل غير مهيأ.');
    }
    final token = await _storage.read(key: _key);
    if (token == null || token.isEmpty) {
      throw const ApiException('أدخل مفتاح الطوارئ أولًا.');
    }
    _dio.options.headers['Authorization'] = 'Bearer $token';
    try {
      final result = await call();
      return result.data ?? <String, dynamic>{};
    } on DioException catch (error) {
      final raw = error.response?.data;
      final detail = raw is Map ? raw['detail']?.toString() : null;
      throw ApiException(
        detail?.isNotEmpty == true
            ? detail!
            : 'تعذر الوصول إلى خدمة الطوارئ أو Hetzner.',
        statusCode: error.response?.statusCode,
      );
    }
  }
}
