import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/api_client.dart';
import 'package:tawtheeq_operations/src/models.dart';
import 'package:tawtheeq_operations/src/screens/action_history_screen.dart';
import 'package:tawtheeq_operations/src/screens/analytics_panel.dart';
import 'package:tawtheeq_operations/src/screens/project_logs_screen.dart';
import 'package:tawtheeq_operations/src/state.dart';

Map<String, dynamic> _analyticsPayload(String window) => {
  'window': window,
  'source': 'host',
  'start': '2026-10-10T12:00:00Z',
  'end': '2026-10-12T12:00:00Z',
  'coverage': {
    'sample_count': 500,
    'expected_samples': 576,
    'coverage_percent': 86.8,
    'stale': true,
    'last_captured_at': '2026-10-12T11:00:00Z',
  },
  'summary': {
    'cpu_avg': 20.0,
    'cpu_max': 65.5,
    'memory_avg': 60.0,
    'memory_max': 71.0,
    'disk_latest': 35.1,
    'availability_percent': 99.9,
    'health_checks': 1000,
    'failed_checks': 1,
    'latency_avg_ms': 134.5,
    'latency_p95_ms': 301.0,
  },
  'series': [
    {
      'captured_at': '2026-10-10T12:00:00Z',
      'cpu_percent': 10.0,
      'memory_percent': 55.0,
    },
    {
      'captured_at': '2026-10-12T12:00:00Z',
      'cpu_percent': 30.0,
      'memory_percent': 65.0,
    },
  ],
  'latest': {
    'host_details': {'cpu_count': 4, 'memory_available_mb': 2500},
    'container_states': [
      {
        'name': 'mowadhabah-production-web-1',
        'state': 'running',
        'health': 'healthy',
        'cpu_percent': 10.0,
        'memory_used_mb': 350.0,
      },
    ],
  },
  'projects': [
    for (var i = 1; i <= 5; i++)
      {
        'id': i,
        'name': 'مشروع $i',
        'summary': {
          'cpu_avg': 4.5,
          'memory_avg': 11.0,
          'availability_percent': 100.0,
        },
        'coverage': {'coverage_percent': 99.0},
      },
  ],
};

OperationActionInfo _action(String status, {int id = 3, bool hasMore = true}) =>
    OperationActionInfo.fromJson({
      'id': id,
      'request_id': 'request-$id',
      'action': 'read_logs',
      'action_label': 'قراءة السجلات',
      'status': status,
      'result_summary': status == 'succeeded'
          ? 'اكتملت القراءة'
          : 'بانتظار الوكيل',
      'service_name': 'خدمة الويب',
      'requested_by_name': 'مدير العمليات',
      'requested_at': '2026-10-12T12:00:00Z',
      'log_content':
          '2026-10-12T12:00:00Z ERROR sample\n2026-10-12T12:01:00Z INFO ready',
      'log_analysis': {'lines': 2, 'errors': 1, 'warnings': 0},
      'log_page': {
        'since_at': '2026-10-10T12:00:00Z',
        'next_until': hasMore ? '2026-10-12T11:59:59Z' : '',
        'has_more': hasMore,
        'truncated': true,
      },
    });

class MonitoringApi extends OperationsApi {
  final List<String> windows = [];
  final List<Map<String, dynamic>> requests = [];
  bool failAnalytics = false;
  bool failDashboard = false;
  bool logsHaveMore = true;
  int dashboardCalls = 0;
  int detailCalls = 0;
  @override
  Future<AnalyticsData> analytics(
    int id, {
    required bool server,
    String window = '48h',
  }) async {
    windows.add(window);
    if (failAnalytics) throw const ApiException('الاتصال غير متاح');
    return AnalyticsData.fromJson(_analyticsPayload(window));
  }

  @override
  Future<OperationActionInfo> runAction(
    int projectId,
    String action, {
    int? serviceId,
    String? confirmation,
    int? sinceMinutes,
    int? tail,
    String? until,
    String? sinceAt,
  }) async {
    requests.add({
      'service_id': serviceId,
      'until': until,
      'since_at': sinceAt,
    });
    return _action('queued', id: requests.length);
  }

  @override
  Future<OperationActionInfo> actionDetail(int projectId, int actionId) async {
    detailCalls++;
    return _action('succeeded', id: actionId, hasMore: logsHaveMore);
  }

  @override
  Future<DashboardData> dashboard() async {
    dashboardCalls++;
    if (failDashboard) throw const ApiException('الاتصال غير متاح');
    return DashboardData.fromJson({
      'summary': {'projects': 5},
      'servers': [],
      'incidents': [],
      'current_user': {
        'id': 2,
        'role': 'viewer',
        'capabilities': ['view'],
      },
    });
  }
}

Widget _app(MonitoringApi api, Widget child) => ProviderScope(
  overrides: [apiProvider.overrideWithValue(api)],
  child: RepaintBoundary(
    key: const ValueKey('qa-capture'),
    child: MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        fontFamily: 'Tajawal',
        colorSchemeSeed: const Color(0xFF07583A),
        useMaterial3: true,
      ),
      home: Directionality(textDirection: TextDirection.rtl, child: child),
    ),
  ),
);

void main() {
  setUpAll(() async {
    if (const bool.fromEnvironment('CAPTURE_OPS_UI')) {
      final loader = FontLoader('Tajawal')
        ..addFont(rootBundle.load('assets/fonts/Tajawal-Regular.ttf'));
      await loader.load();
      final icons = FontLoader('MaterialIcons')
        ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
      await icons.load();
    }
  });
  test('analytics preserves missing values and terminal action metadata', () {
    final analytics = AnalyticsData.fromJson({
      'coverage': {'stale': true},
      'summary': {'cpu_avg': null},
    });
    expect(analytics.summary['cpu_avg'], isNull);
    expect(analytics.stale, isTrue);
    expect(_action('succeeded').finished, isTrue);
    expect(_action('queued').finished, isFalse);
    expect(_action('succeeded').serviceName, 'خدمة الويب');
  });

  test('dashboard keeps last good data after refresh failure', () async {
    final api = MonitoringApi();
    final controller = DashboardController(api, () async {});
    await Future<void>.delayed(Duration.zero);
    expect(controller.state.requireValue.projectCount, 5);
    api.failDashboard = true;
    await controller.refresh();
    expect(controller.state.requireValue.projectCount, 5);
    expect(controller.refreshError, 'الاتصال غير متاح');
    controller.dispose();
  });

  testWidgets(
    'analytics compares five projects without overflow and retains selected range on failure',
    (tester) async {
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      tester.view.devicePixelRatio = 1;
      final api = MonitoringApi();
      for (final width in [375.0, 768.0, 1280.0]) {
        tester.view.physicalSize = Size(width, 1000);
        await tester.pumpWidget(
          _app(
            api,
            const Scaffold(
              body: SingleChildScrollView(
                child: AnalyticsPanel(id: 1, server: true),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull, reason: 'overflow at $width');
        expect(find.text('مقارنة المشاريع (5)'), findsOneWidget);
        await _capture(tester, width);
      }
      await tester.tap(find.text('7 أيام'));
      await tester.pumpAndSettle();
      expect(api.windows.last, '7d');
      api.failAnalytics = true;
      await tester.tap(find.byTooltip('تحديث التحليلات'));
      await tester.pumpAndSettle();
      expect(find.textContaining('تبقى آخر قراءة ناجحة'), findsOneWidget);
      expect(find.text('مقارنة المشاريع (5)'), findsOneWidget);
    },
  );

  testWidgets(
    'action detail follows existing request to terminal state without submitting again',
    (tester) async {
      final api = MonitoringApi();
      await tester.pumpWidget(
        _app(
          api,
          ActionDetailScreen(projectId: 1, initialAction: _action('queued')),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('اكتمل بنجاح'), findsOneWidget);
      expect(api.detailCalls, 1);
      await tester.pump(const Duration(seconds: 10));
      expect(api.detailCalls, 1);
      expect(api.requests, isEmpty);
    },
  );

  testWidgets('older logs keep original time boundary and filters', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(768, 1800);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final api = MonitoringApi();
    final project = ProjectInfo.fromJson({
      'id': 1,
      'name': 'مواظبة',
      'slug': 'mowadhabah',
      'services': [
        {'id': 5, 'name': 'الويب', 'service_key': 'web'},
      ],
    });
    await tester.pumpWidget(_app(api, ProjectLogsScreen(project: project)));
    await tester.tap(find.text('قراءة وتحليل السجلات'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 2));
    await tester.pumpAndSettle();
    expect(find.textContaining('هذه ليست كل سجلات الفترة'), findsOneWidget);
    await tester.ensureVisible(find.text('تحميل صفحة أقدم'));
    api.logsHaveMore = false;
    await tester.tap(find.text('تحميل صفحة أقدم'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 2));
    await tester.pumpAndSettle();
    expect(api.requests.last['until'], '2026-10-12T11:59:59Z');
    expect(api.requests.last['since_at'], '2026-10-10T12:00:00Z');
    expect(api.requests.last['service_id'], 5);
    expect(find.text('الصفحات المسترجعة: 2'), findsOneWidget);
    expect(find.text('تحميل صفحة أقدم'), findsNothing);
    expect(tester.takeException(), isNull);
  });
}

Future<void> _capture(WidgetTester tester, double width) async {
  if (!const bool.fromEnvironment('CAPTURE_OPS_UI')) return;
  final boundary = tester.renderObject<RenderRepaintBoundary>(
    find.byKey(const ValueKey('qa-capture')),
  );
  await tester.runAsync(() async {
    final rendered = await boundary.toImage(pixelRatio: 1);
    final bytes = await rendered.toByteData(format: ui.ImageByteFormat.png);
    final file = File('build/qa/analytics-${width.toInt()}.png');
    await file.parent.create(recursive: true);
    await file.writeAsBytes(bytes!.buffer.asUint8List());
    rendered.dispose();
  });
}
