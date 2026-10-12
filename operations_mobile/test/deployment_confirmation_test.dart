import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:tawtheeq_operations/src/api_client.dart';
import 'package:tawtheeq_operations/src/design_system.dart';
import 'package:tawtheeq_operations/src/models.dart';
import 'package:tawtheeq_operations/src/screens/dashboard_screen.dart';
import 'package:tawtheeq_operations/src/state.dart';
import 'package:tawtheeq_operations/src/widgets/deployment_confirmation.dart';

const _latestSha = 'a5e1234b5678901234567890123456789012345678';
const _deployedSha = 'f5f59920644666153f620090b8a6a9b73f6c0908';

DeploymentInfo _release({bool canDeploy = true, bool shortOnly = false}) =>
    DeploymentInfo.fromJson({
      'project_id': 1,
      'project_name': 'توثيق',
      'repository': 'example/school-reports',
      'branch': 'main',
      'configured': true,
      'deployment_enabled': canDeploy,
      'can_deploy': canDeploy,
      'repository_ahead': true,
      'latest_sha': shortOnly ? '' : _latestSha,
      'latest_short_sha': 'a5e1234',
      'deployed_sha': shortOnly ? '' : _deployedSha,
      'deployed_short_sha': 'f5f5992',
      'deployed_reference': 'f5f5992',
      'workflow_status': 'completed',
      'workflow_conclusion': 'success',
      'action_required': 'إصدار جديد جاهز للمراجعة',
    });

class _DeploymentApi extends OperationsApi {
  _DeploymentApi({this.canRunActions = true, DeploymentInfo? release})
    : release = release ?? _release();

  final bool canRunActions;
  final DeploymentInfo release;
  final requests = <Map<String, Object>>[];
  final pending = Completer<DeploymentInfo>();

  @override
  Future<DashboardData> dashboard() async => DashboardData.fromJson({
    'summary': {'projects': 1, 'healthy_projects': 1},
    'servers': [],
    'incidents': [],
    'current_user': {
      'id': 2,
      'name': 'مشغل العمليات',
      'role': canRunActions ? 'operator' : 'viewer',
      'capabilities': ['view', if (canRunActions) 'run_actions'],
    },
  });

  @override
  Future<DeploymentOverview> deploymentStatus() async => DeploymentOverview(
    deployments: [release],
    repositoryAheadCount: 1,
    canDeployCount: release.canDeploy ? 1 : 0,
  );

  @override
  Future<DeploymentInfo> triggerDeployment({
    required int projectId,
    required String confirmation,
  }) {
    requests.add({'project_id': projectId, 'confirmation': confirmation});
    return pending.future;
  }
}

Widget _app(_DeploymentApi api) => ProviderScope(
  overrides: [
    apiProvider.overrideWithValue(api),
    dashboardProvider.overrideWith(
      (ref) => DashboardController(api, () async {}),
    ),
  ],
  child: MaterialApp(
    theme: ThemeData(
      fontFamily: 'Tajawal',
      extensions: const [OpsPalette.light],
    ),
    builder: (context, child) =>
        Directionality(textDirection: TextDirection.rtl, child: child!),
    home: const DashboardScreen(),
  ),
);

Future<void> _tapVisible(WidgetTester tester, Finder target) async {
  // Scrolling updates layout on the next frame. Center the target in its
  // viewport so the app bar, navigation bar and SnackBars cannot cover the tap.
  await Scrollable.ensureVisible(tester.element(target), alignment: 0.5);
  await tester.pumpAndSettle();
  expect(target.hitTestable(), findsOneWidget);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

Future<void> _openConfirmation(WidgetTester tester) async {
  await _tapVisible(tester, find.byKey(const ValueKey('deploy-1')));
  expect(find.byType(DeploymentConfirmationDialog), findsOneWidget);
}

void main() {
  setUpAll(() async {
    final font = FontLoader('Tajawal')
      ..addFont(rootBundle.load('assets/fonts/Tajawal-Regular.ttf'));
    await font.load();
  });

  testWidgets('cancel, back and barrier dismissal never send a deployment', (
    tester,
  ) async {
    final api = _DeploymentApi();
    await tester.pumpWidget(_app(api));
    await tester.pumpAndSettle();

    await _openConfirmation(tester);
    expect(find.text('توثيق'), findsWidgets);
    expect(find.text('example/school-reports'), findsOneWidget);
    expect(find.text('main'), findsOneWidget);
    expect(find.text(_latestSha), findsOneWidget);
    expect(
      tester
          .widget<TextField>(
            find.byKey(const ValueKey('deployment-confirmation-input')),
          )
          .controller!
          .text,
      isEmpty,
    );
    await tester.tap(find.byKey(const ValueKey('cancel-deployment')));
    await tester.pumpAndSettle();

    await _openConfirmation(tester);
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();

    await _openConfirmation(tester);
    await tester.tapAt(const Offset(4, 4));
    await tester.pumpAndSettle();
    expect(find.byType(DeploymentConfirmationDialog), findsNothing);
    expect(api.requests, isEmpty);
    expect(
      tester
          .widget<ElevatedButton>(find.byKey(const ValueKey('deploy-1')))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets(
    'exact typed SHA authorizes one call and pending blocks duplicates',
    (tester) async {
      final api = _DeploymentApi();
      await tester.pumpWidget(_app(api));
      await tester.pumpAndSettle();
      final deploy = find.byKey(const ValueKey('deploy-1'));
      await tester.ensureVisible(deploy);
      // Invoke the same pre-rebuild callback twice to cover rapid taps.
      final open = tester.widget<ElevatedButton>(deploy).onPressed!;
      open();
      open();
      await tester.pumpAndSettle();
      expect(find.byType(DeploymentConfirmationDialog), findsOneWidget);
      expect(api.requests, isEmpty);

      final input = find.byKey(const ValueKey('deployment-confirmation-input'));
      final confirm = find.byKey(const ValueKey('confirm-deployment'));
      for (final wrong in ['', 'wrong', 'A5E1234', 'a5e1234 ']) {
        await tester.enterText(input, wrong);
        await tester.pump();
        expect(tester.widget<FilledButton>(confirm).onPressed, isNull);
      }
      expect(api.requests, isEmpty);
      await tester.enterText(input, 'a5e1234');
      await tester.pump();
      final submit = tester.widget<FilledButton>(confirm).onPressed!;
      submit();
      submit();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(api.requests, [
        {'project_id': 1, 'confirmation': 'a5e1234'},
      ]);
      expect(tester.widget<ElevatedButton>(deploy).onPressed, isNull);
      open();
      await tester.pump();
      expect(api.requests, hasLength(1));
      expect(find.byType(DeploymentConfirmationDialog), findsNothing);

      api.pending.completeError(const ApiException('تعذر النشر التجريبي'));
      await tester.pumpAndSettle();
      expect(tester.widget<ElevatedButton>(deploy).onPressed, isNotNull);
      expect(find.text('تعذر النشر التجريبي'), findsOneWidget);
    },
  );

  testWidgets('viewer permissions and backend deployment gate stay enforced', (
    tester,
  ) async {
    for (final api in [
      _DeploymentApi(canRunActions: false),
      _DeploymentApi(release: _release(canDeploy: false)),
    ]) {
      await tester.pumpWidget(const SizedBox());
      await tester.pumpWidget(_app(api));
      await tester.pumpAndSettle();
      final deploy = find.byKey(const ValueKey('deploy-1'));
      await tester.ensureVisible(deploy);
      expect(tester.widget<ElevatedButton>(deploy).onPressed, isNull);
      expect(api.requests, isEmpty);
    }
  });

  testWidgets(
    'phone release rows preserve seven characters and copy full SHAs',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(360, 900);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final clipboard = <String>[];
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') {
            clipboard.add((call.arguments as Map)['text'] as String);
          }
          return null;
        },
      );
      addTearDown(() {
        tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        );
      });

      await tester.pumpWidget(_app(_DeploymentApi()));
      await tester.pumpAndSettle();
      for (final sha in ['f5f5992', 'a5e1234']) {
        final reference = find.widgetWithText(SelectableText, sha);
        expect(reference, findsOneWidget);
        await tester.ensureVisible(reference);
        expect(tester.getSize(reference).width, greaterThan(80));
      }
      for (final source in ['الخادم', 'GitHub']) {
        final copy = find.byTooltip('نسخ مرجع $source');
        await _tapVisible(tester, copy);
      }
      expect(clipboard, [_deployedSha, _latestSha]);
      expect(tester.takeException(), isNull);

      await _openConfirmation(tester);
      expect(find.text(_latestSha), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets(
    'missing full revision copies only the real available reference',
    (tester) async {
      String? copied;
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') {
            copied = (call.arguments as Map)['text'] as String;
          }
          return null;
        },
      );
      addTearDown(() {
        tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        );
      });
      await tester.pumpWidget(
        _app(_DeploymentApi(release: _release(shortOnly: true))),
      );
      await tester.pumpAndSettle();
      final copy = find.byTooltip('نسخ مرجع GitHub');
      await _tapVisible(tester, copy);
      expect(copied, 'a5e1234');
    },
  );

  testWidgets(
    'server-managed releases copy full SHA then image then available reference',
    (tester) async {
      const image =
          'registry.example.com/mizaan/web:authority-scope-20261012-00b754f-extra-long-release-tag';
      const reference = 'authority-scope-20261012-00b754f';
      String? copied;
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') {
            copied = (call.arguments as Map)['text'] as String;
          }
          return null;
        },
      );
      addTearDown(() {
        tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        );
      });
      for (final expected in [_deployedSha, image, reference]) {
        await tester.pumpWidget(const SizedBox());
        final release = DeploymentInfo.fromJson({
          'project_id': 1,
          'project_name': 'ميزان',
          'monitoring_mode': 'server',
          'deployed_sha': expected == _deployedSha ? _deployedSha : '',
          'deployed_image': expected == reference ? '' : image,
          'deployed_reference': reference,
        });
        await tester.pumpWidget(_app(_DeploymentApi(release: release)));
        await tester.pumpAndSettle();
        final copy = find.byTooltip('نسخ مرجع الخادم');
        await _tapVisible(tester, copy);
        expect(copied, expected);
        expect(find.byKey(const ValueKey('deploy-1')), findsNothing);
        expect(tester.takeException(), isNull);
      }
    },
  );
}
