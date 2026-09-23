import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/api_client.dart';
import 'package:tawtheeq_operations/src/models.dart';
import 'package:tawtheeq_operations/src/screens/payment_links_screen.dart';
import 'package:tawtheeq_operations/src/state.dart';

PaymentLinksData _paymentLinksFixture() => PaymentLinksData.fromJson({
  'gateway_enabled': true,
  'can_manage': true,
  'projects': [
    {
      'id': 9,
      'name': 'منصة تقنية ذات اسم طويل لاختبار العرض المتجاوب',
      'slug': 'tech',
    },
  ],
  'payment_links': [
    {
      'public_id': '11111111-1111-1111-1111-111111111111',
      'project_id': 9,
      'project_name': 'منصة تقنية ذات اسم طويل لاختبار العرض المتجاوب',
      'customer_name': 'عميل تجريبي ذو اسم عربي طويل',
      'customer_phone': '+966500000000',
      'customer_email': 'customer@example.com',
      'amount': '125.50',
      'currency': 'SAR',
      'description':
          'خدمة تطوير ودعم تقني مستمر تتضمن وصفًا طويلًا لاختبار التفاف النص.',
      'internal_reference': 'QUOTE-2026-00000015',
      'gateway_url': 'https://checkout.moyasar.com/invoices/example',
      'status': 'initiated',
      'status_label': 'بانتظار الدفع',
      'created_by_name': 'مدير العمليات',
      'can_cancel': true,
      'cancellation_confirmation': '11111111',
      'expires_at': '2026-10-01T12:00:00Z',
    },
  ],
});

Widget _paymentLinksApp() => ProviderScope(
  overrides: [
    paymentLinksProvider.overrideWith((ref) async => _paymentLinksFixture()),
  ],
  child: const MaterialApp(
    home: Directionality(
      textDirection: TextDirection.rtl,
      child: PaymentLinksScreen(),
    ),
  ),
);

void main() {
  test('accepts only a non-empty access token', () {
    expect(parseOperationsToken({'token': '  secure-token  '}), 'secure-token');
    expect(
      () => parseOperationsToken({'token': null}),
      throwsA(isA<ApiException>()),
    );
    expect(
      () => parseOperationsToken({'token': ''}),
      throwsA(isA<ApiException>()),
    );
  });

  test('prefers a provisioned device token over stale local storage', () {
    expect(
      resolveOperationsToken(
        storedToken: 'old-token',
        provisionedToken: '  device-token  ',
      ),
      'device-token',
    );
    expect(
      resolveOperationsToken(storedToken: ' stored ', provisionedToken: ''),
      'stored',
    );
    expect(
      resolveOperationsToken(storedToken: null, provisionedToken: ''),
      isNull,
    );
  });

  test(
    'maps backend health states without treating unknown values as healthy',
    () {
      expect(healthStatusFrom('healthy'), HealthStatus.healthy);
      expect(healthStatusFrom('down'), HealthStatus.down);
      expect(healthStatusFrom('unexpected'), HealthStatus.unknown);
    },
  );

  test('parses a dashboard inventory payload', () {
    final dashboard = DashboardData.fromJson({
      'summary': {
        'servers': 1,
        'projects': 1,
        'healthy_projects': 1,
        'open_incidents': 0,
      },
      'servers': [
        {
          'id': 1,
          'name': 'main',
          'provider': 'hetzner',
          'status': 'healthy',
          'projects': [
            {
              'id': 7,
              'name': 'Project',
              'slug': 'project',
              'base_url': 'https://example.com',
              'status': 'healthy',
              'services': [],
            },
          ],
        },
      ],
      'incidents': [],
      'current_user': {
        'id': 3,
        'name': 'Ops',
        'phone': '0500000000',
        'role': 'operator',
        'role_label': 'مشغّل',
        'capabilities': ['view', 'run_checks'],
      },
      'agent': {'ready': false, 'label': 'غير مفعّل'},
      'generated_at': '2026-08-21T12:00:00Z',
    });

    expect(dashboard.serverCount, 1);
    expect(dashboard.servers.single.projects.single.id, 7);
    expect(dashboard.generatedAt, isNotNull);
    expect(dashboard.currentUser.can('run_checks'), isTrue);
    expect(dashboard.currentUser.can('manage_team'), isFalse);
    expect(dashboard.agentReady, isFalse);
  });

  test('parses a server-managed deployment release reference', () {
    final info = DeploymentInfo.fromJson({
      'project_id': 4,
      'project_slug': 'mizaan-beta',
      'project_name': 'Mizaan Beta',
      'deployed_image': 'mizaan-beta-web:authority-scope-20260905-00b754f',
      'deployed_reference': 'authority-scope-20260905-00b754f',
      'monitoring_mode': 'server',
    });

    expect(info.monitoringMode, 'server');
    expect(info.deployedReference, 'authority-scope-20260905-00b754f');
    expect(info.repository, isEmpty);
    expect(info.canDeploy, isFalse);
  });

  test(
    'keeps legacy superuser access and labels a missing agent as disabled',
    () {
      final dashboard = DashboardData.fromJson({
        'summary': const {},
        'servers': const [],
        'incidents': const [],
      });

      expect(dashboard.currentUser.isSuperuser, isTrue);
      expect(dashboard.currentUser.can('run_checks'), isTrue);
      expect(dashboard.currentUser.can('manage_team'), isTrue);
      expect(dashboard.agentReady, isFalse);
      expect(dashboard.agentLabel, 'غير مفعّل');
    },
  );

  test('parses payment links and builds an encoded WhatsApp handoff', () {
    final data = _paymentLinksFixture();

    final link = data.links.single;
    final uri = paymentLinkWhatsAppUri(link);
    expect(data.projects.single.id, 9);
    expect(link.isPayable, isTrue);
    expect(normalizeWhatsAppPhone(link.customerPhone), '966500000000');
    expect(uri.host, 'wa.me');
    expect(uri.path, '/966500000000');
    expect(uri.queryParameters['text'], contains(link.gatewayUrl));
    expect(uri.queryParameters['text'], contains('125.50 SAR'));
  });

  testWidgets(
    'payment links screen renders without overflow at target widths',
    (tester) async {
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      tester.view.devicePixelRatio = 1;

      for (final width in [375.0, 768.0, 1024.0, 1280.0, 1440.0]) {
        tester.view.physicalSize = Size(width, 900);
        await tester.pumpWidget(_paymentLinksApp());
        await tester.pumpAndSettle();
        expect(
          tester.takeException(),
          isNull,
          reason: 'unexpected layout error at ${width.toInt()} px',
        );
      }
    },
  );

  testWidgets('create payment link form is reachable and usable on a phone', (
    tester,
  ) async {
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(375, 900);
    await tester.pumpWidget(_paymentLinksApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('رابط جديد'));
    await tester.pumpAndSettle();

    expect(find.text('بيانات التحصيل'), findsOneWidget);
    expect(find.text('جوال العميل مع مفتاح الدولة'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
