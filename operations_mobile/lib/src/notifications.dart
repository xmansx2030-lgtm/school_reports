import 'dart:async';
import 'dart:math';

import 'package:device_info_plus/device_info_plus.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'api_client.dart';

int? operationsIncidentId(Map<String, dynamic> data) {
  if (data['type'] != 'operations_incident') return null;
  final id = int.tryParse('${data['incident_id'] ?? ''}');
  return id != null && id > 0 ? id : null;
}

class NotificationService {
  NotificationService(this._api, {required this.onIncidentOpened});
  final OperationsApi _api;
  final ValueChanged<int> onIncidentOpened;
  final _local = FlutterLocalNotificationsPlugin();
  static const _storage = FlutterSecureStorage(aOptions: AndroidOptions());
  static const _deviceKey = 'operations_installation_id';
  final _subscriptions = <StreamSubscription<dynamic>>[];
  Future<void>? _initializing;
  Future<void>? _registration;
  String? _deviceId;
  bool _checkedLocalLaunch = false;
  int _generation = 0;

  Future<void> initialize() {
    if (_subscriptions.isNotEmpty) return Future.value();
    return _initializing ??= _initialize(_generation).whenComplete(() {
      _initializing = null;
    });
  }

  Future<void> _initialize(int generation) async {
    try {
      if (Firebase.apps.isEmpty) await Firebase.initializeApp();
      if (generation != _generation) return;
      final messaging = FirebaseMessaging.instance;
      await _local.initialize(
        settings: const InitializationSettings(
          android: AndroidInitializationSettings('@mipmap/ic_launcher'),
        ),
        onDidReceiveNotificationResponse: (response) {
          final id = int.tryParse(response.payload ?? '');
          if (generation == _generation && id != null && id > 0) {
            onIncidentOpened(id);
          }
        },
      );
      const channel = AndroidNotificationChannel(
        'operations_alerts',
        'تنبيهات الخادم',
        description: 'تنبيهات الأعطال والحالات الحرجة للمشاريع',
        importance: Importance.max,
      );
      await _local
          .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin
          >()
          ?.createNotificationChannel(channel);
      if (generation != _generation) return;
      final permission = await messaging.requestPermission();
      if (generation != _generation) return;
      if (permission.authorizationStatus == AuthorizationStatus.denied) return;
      final token = await messaging.getToken();
      if (generation != _generation) return;
      if (token != null) await _register(token, generation);
      if (generation != _generation) return;

      void open(RemoteMessage message) {
        final id = operationsIncidentId(message.data);
        if (generation == _generation && id != null) onIncidentOpened(id);
      }

      _subscriptions.addAll([
        messaging.onTokenRefresh.listen((token) {
          unawaited(_register(token, generation));
        }),
        FirebaseMessaging.onMessage.listen((message) {
          if (generation == _generation) unawaited(_show(message));
        }),
        FirebaseMessaging.onMessageOpenedApp.listen(open),
      ]);
      final initial = await messaging.getInitialMessage();
      if (initial != null) open(initial);
      if (!_checkedLocalLaunch && generation == _generation) {
        _checkedLocalLaunch = true;
        final launch = await _local.getNotificationAppLaunchDetails();
        final id = int.tryParse(launch?.notificationResponse?.payload ?? '');
        if (generation == _generation &&
            launch?.didNotificationLaunchApp == true &&
            id != null &&
            id > 0) {
          onIncidentOpened(id);
        }
      }
    } catch (error) {
      debugPrint('Operations push setup failed: ${error.runtimeType}');
    }
  }

  Future<void> _show(RemoteMessage message) async {
    final notification = message.notification;
    if (notification == null) return;
    final id = operationsIncidentId(message.data);
    try {
      await _local.show(
        id: id == null ? notification.hashCode : id & 0x7fffffff,
        title: notification.title,
        body: notification.body,
        payload: id?.toString(),
        notificationDetails: NotificationDetails(
          android: AndroidNotificationDetails(
            'operations_alerts',
            'تنبيهات الخادم',
            channelDescription: 'تنبيهات الأعطال والحالات الحرجة للمشاريع',
            importance: Importance.max,
            priority: Priority.high,
            onlyAlertOnce: message.data['severity'] != 'critical',
          ),
        ),
      );
    } catch (error) {
      debugPrint('Operations push display failed: ${error.runtimeType}');
    }
  }

  Future<void> _register(String token, int generation) {
    final pending = _registration;
    return _registration = _registerAfter(pending, token, generation);
  }

  Future<void> _registerAfter(
    Future<void>? pending,
    String token,
    int generation,
  ) async {
    await pending;
    if (generation != _generation) return;
    try {
      final info = await DeviceInfoPlugin().androidInfo;
      final deviceId = await _installationId();
      if (generation != _generation) return;
      _deviceId = deviceId;
      await _api.registerDevice(
        deviceId: deviceId,
        name: '${info.brand} ${info.model}'.trim(),
        fcmToken: token,
      );
    } catch (error) {
      debugPrint('Operations device registration failed: ${error.runtimeType}');
    }
  }

  Future<String> _installationId() async {
    final stored = await _storage.read(key: _deviceKey);
    if (stored != null && stored.isNotEmpty) return stored;
    final random = Random.secure();
    final created = List.generate(
      16,
      (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
    await _storage.write(key: _deviceKey, value: created);
    return created;
  }

  Future<void> stop({bool unregister = false}) async {
    _generation++;
    final subscriptions = _subscriptions.toList();
    _subscriptions.clear();
    await Future.wait(
      subscriptions.map((subscription) => subscription.cancel()),
    );
    if (!unregister) return;
    await _registration;
    try {
      await _local.cancelAll();
    } catch (error) {
      debugPrint(
        'Operations notification cleanup failed: ${error.runtimeType}',
      );
    }
    try {
      final deviceId = _deviceId ?? await _storage.read(key: _deviceKey);
      if (deviceId == null) {
        if (Firebase.apps.isNotEmpty) {
          await FirebaseMessaging.instance.deleteToken();
        }
        return;
      }
      await _api.unregisterDevice(deviceId);
    } catch (error) {
      debugPrint(
        'Operations device unregistration failed: ${error.runtimeType}',
      );
      if (Firebase.apps.isNotEmpty) {
        try {
          await FirebaseMessaging.instance.deleteToken();
        } catch (error) {
          debugPrint(
            'Operations push token removal failed: ${error.runtimeType}',
          );
        }
      }
    }
  }

  void dispose() => unawaited(stop());
}
