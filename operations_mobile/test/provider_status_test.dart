import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/models.dart';
import 'package:tawtheeq_operations/src/widgets/provider_status.dart';

void main() {
  test('selects the seven newest backups before limiting without mutation', () {
    final backups = <Map<String, dynamic>>[
      {'id': 'unknown', 'created': 'invalid'},
      for (var day = 1; day <= 9; day++)
        {
          'id': day,
          'created': '2026-10-${day.toString().padLeft(2, '0')}T22:00:00Z',
          'description': 'backup-$day',
          'status': 'available',
        },
    ];
    final result = latestProviderBackups(backups);
    expect(result.map((item) => item['id']), [9, 8, 7, 6, 5, 4, 3]);
    expect(backups.first['id'], 'unknown');
    expect(backups[1]['id'], 1);
    expect(result.first, same(backups.last));
  });

  test('sorts offset dates by instant and keeps missing dates last', () {
    final result = latestProviderBackups([
      {'id': 1, 'created': '2026-10-12T03:00:00+03:00'},
      {'id': 2, 'created': '2026-10-12T01:00:00Z'},
      {'id': 3},
    ]);
    expect(result.map((item) => item['id']), [2, 1, 3]);
  });

  test('formats UTC and DateTime values in device local time', () {
    final instant = DateTime.parse('2026-10-12T23:30:00Z');
    final expected = instant
        .toLocal()
        .toIso8601String()
        .substring(0, 16)
        .replaceAll('-', '/')
        .replaceFirst('T', ' ');
    expect(providerLocalDate('2026-10-12T23:30:00Z'), expected);
    expect(providerLocalDate(instant), expected);
    expect(providerLocalDate('invalid'), 'غير متاح');
    expect(providerLocalDate(null), 'غير متاح');
    expect(providerLocalDate(DateTime(2026, 1, 2, 3, 4)), '2026/01/02 03:04');
  });

  testWidgets('localizes automatic backup titles and preserves custom names', (
    tester,
  ) async {
    const automatic = 'Backup 2026-10-12T18:44:11Z';
    const custom = 'نسخة قبل تحديث توثيق';
    const customWithPrefix = 'Backup before migration';
    await tester.pumpWidget(
      const MaterialApp(
        home: Directionality(
          textDirection: TextDirection.rtl,
          child: Scaffold(
            body: SingleChildScrollView(
              child: ProviderBackupList(
                backups: [
                  {'description': automatic, 'created': '2026-10-12T18:44:11Z'},
                  {'description': custom},
                  {'description': customWithPrefix},
                ],
              ),
            ),
          ),
        ),
      ),
    );
    expect(find.text('نسخة احتياطية'), findsOneWidget);
    expect(find.text(automatic), findsNothing);
    expect(find.byTooltip(automatic), findsOneWidget);
    expect(find.text(custom), findsOneWidget);
    expect(find.text(customWithPrefix), findsOneWidget);
    expect(
      find.text(providerLocalDate('2026-10-12T18:44:11Z')),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  for (final scale in [1.0, 2.0]) {
    testWidgets('provider values remain LTR at 360dp and text scale $scale', (
      tester,
    ) async {
      await tester.binding.setSurfaceSize(const Size(360, 800));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      final provider = ProviderServerInfo.fromJson({
        'name': 'school-reports-prod',
        'status': 'running',
        'server_type': 'cpx32',
        'location': 'nbg1',
        'fetched_at': '2026-10-12T20:00:00Z',
        'metrics': {
          'time_series': {
            'cpu': {
              'values': [
                [1791835140, '45.2'],
              ],
            },
          },
        },
      });
      await tester.pumpWidget(
        MaterialApp(
          home: MediaQuery(
            data: MediaQueryData(textScaler: TextScaler.linear(scale)),
            child: Directionality(
              textDirection: TextDirection.rtl,
              child: Scaffold(
                body: SingleChildScrollView(
                  child: Padding(
                    padding: const EdgeInsets.all(34),
                    child: ProviderStatusSummary(
                      provider: provider,
                      showName: true,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      );
      expect(tester.takeException(), isNull);
      expect(
        tester.widget<Text>(find.text('45.2%')).textDirection,
        TextDirection.ltr,
      );
      expect(find.text('المصدر: Hetzner'), findsOneWidget);
      expect(find.text('وقت القياس'), findsOneWidget);
      expect(find.text('آخر جلب'), findsOneWidget);
      expect(
        tester.widget<Text>(find.text('school-reports-prod')).textDirection,
        TextDirection.ltr,
      );
    });
  }
}
