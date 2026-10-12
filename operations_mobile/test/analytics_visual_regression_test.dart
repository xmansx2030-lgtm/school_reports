import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/api_client.dart';
import 'package:tawtheeq_operations/src/models.dart';
import 'package:tawtheeq_operations/src/screens/analytics_panel.dart';
import 'package:tawtheeq_operations/src/state.dart';

class _AnalyticsApi extends OperationsApi {
  @override
  Future<AnalyticsData> analytics(
    int id, {
    required bool server,
    String window = '48h',
  }) async {
    final start = DateTime(2026, 10, 10, 23, 30);
    final end = start.add(
      Duration(
        hours: window == '24h'
            ? 24
            : window == '1h'
            ? 1
            : 48,
      ),
    );
    return AnalyticsData.fromJson({
      'window': window,
      'source': 'docker',
      'start': start.toIso8601String(),
      'end': end.toIso8601String(),
      'coverage': {'coverage_percent': 100, 'sample_count': 500},
      'summary': {
        'cpu_avg': 18.7,
        'cpu_max': 58.5,
        'memory_avg': 61.7,
        'memory_max': 64.1,
        'disk_latest': 34,
        'availability_percent': 100,
        'health_checks': 5827,
        'failed_checks': 0,
        'latency_avg_ms': 181.9,
        'latency_p95_ms': 308,
        'memory_used_avg_mb': 1292.2,
        'memory_used_max_mb': 1600.2,
      },
      'series': [
        for (final date in [start, end])
          {
            'captured_at': date.toIso8601String(),
            'cpu_percent': 18.7,
            'memory_percent': 61.7,
          },
      ],
      'latest': {
        'running_container_count': 2,
        'container_count': 2,
        'network_rx_mb': 12456.7,
        'network_tx_mb': 87654.3,
        'block_read_mb': 1256.7,
        'block_write_mb': 8734.3,
        'container_states': [
          {
            'name': 'school-reports-production-web-1',
            'state': 'running',
            'health': 'healthy',
            'cpu_percent': 10,
            'memory_used_mb': 1292.2,
          },
          {
            'name': 'school-reports-production-worker-1',
            'state': 'running',
            'health': '  ',
            'restart_count': 1,
          },
          {
            'name': 'school-reports-production-migrate-1',
            'state': 'exited',
            'completed_job': true,
          },
        ],
      },
    });
  }
}

Widget _app({double textScale = 1}) => ProviderScope(
  overrides: [apiProvider.overrideWithValue(_AnalyticsApi())],
  child: MaterialApp(
    builder: (context, child) => MediaQuery(
      data: MediaQuery.of(
        context,
      ).copyWith(textScaler: TextScaler.linear(textScale)),
      child: Directionality(textDirection: TextDirection.rtl, child: child!),
    ),
    home: const Scaffold(
      body: SingleChildScrollView(
        padding: EdgeInsets.all(16),
        child: AnalyticsPanel(id: 1),
      ),
    ),
  ),
);

void main() {
  testWidgets(
    'phone metrics use two columns and expand for large Arabic text',
    (tester) async {
      tester.view.physicalSize = const Size(360, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final cpu = find.byKey(const ValueKey('analytics-metric-المعالج'));
      final memory = find.byKey(const ValueKey('analytics-metric-الذاكرة'));

      await tester.pumpWidget(_app());
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(cpu).dy, tester.getTopLeft(memory).dy);
      expect(tester.getSize(cpu).width, greaterThanOrEqualTo(140));
      expect(
        tester.getTopLeft(cpu).dx,
        greaterThan(tester.getTopLeft(memory).dx),
      );
      expect(tester.takeException(), isNull);

      await tester.pumpWidget(_app(textScale: 2));
      await tester.pumpAndSettle();
      expect(tester.getTopLeft(cpu).dy, lessThan(tester.getTopLeft(memory).dy));
      expect(tester.getSize(cpu).width, greaterThan(280));
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'chart grid matches percentages and every cross-day range shows dates',
    (tester) async {
      tester.view.physicalSize = const Size(360, 800);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(_app());
      await tester.pumpAndSettle();

      for (final range in ['48 ساعة', '24 ساعة', 'ساعة']) {
        await tester.tap(find.text(range));
        await tester.pumpAndSettle();
        final chart = tester.widget<LineChart>(find.byType(LineChart));
        expect(chart.data.gridData.horizontalInterval, 25);
        expect(chart.data.titlesData.leftTitles.sideTitles.interval, 25);
        final axisDates = find.descendant(
          of: find.byType(LineChart),
          matching: find.byWidgetPredicate(
            (widget) =>
                widget is Text &&
                RegExp(
                  r'^\d{2}/\d{2}\n\d{2}:\d{2}$',
                ).hasMatch(widget.data ?? ''),
          ),
        );
        expect(axisDates, findsWidgets, reason: 'date missing for $range');
        expect(tester.takeException(), isNull);
      }
    },
  );

  testWidgets(
    'container inventory explains completed jobs and missing health checks',
    (tester) async {
      await tester.pumpWidget(_app());
      await tester.pumpAndSettle();
      final services = find.byKey(
        const ValueKey('analytics-metric-الخدمات المستمرة'),
      );
      expect(
        find.descendant(of: services, matching: find.text('تعمل:')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: services, matching: find.text('الإجمالي:')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: services, matching: find.text('2')),
        findsNWidgets(2),
      );
      expect(find.text('حاويات المشروع (3)'), findsOneWidget);
      expect(find.text('خدمات مستمرة: 2 · مهام مكتملة: 1'), findsOneWidget);
      expect(find.text('مهمة مكتملة'), findsOneWidget);
      expect(find.text('لا يوجد فحص صحة'), findsOneWidget);
      expect(find.text('غير محددة'), findsOneWidget);
      expect(find.text('سليمة'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
