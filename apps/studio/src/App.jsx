import React from 'react';
import { BrandWatermark } from './components/BrandWatermark';
import { HeaderBar } from './components/HeaderBar';
import { QuickMenu } from './components/QuickMenu';
import { StateLogsPanel } from './components/StateLogsPanel';
import { TasksPage } from './components/TasksPage';
import { CombinedPage } from './components/CombinedPage';
import { HandGatewayProvider } from './hooks/useHandGatewayContext';
import { SimuPage } from './third_page';
import { HelpCenterModal } from './components/HelpCenterModal';
import { ConfirmDialog } from './components/ConfirmDialog';
import { useMotorStudio } from './hooks/useMotorStudio';
import { MotorStudioProvider } from './hooks/useMotorStudioContext';
import { useI18n } from './i18n';
import appPackage from '../package.json';

export default function App() {
  const { t } = useI18n();
  const studio = useMotorStudio();
  const [page, setPage] = React.useState('combined');
  const [helpOpen, setHelpOpen] = React.useState(false);
  const version = `v${appPackage.version}`;

  const pathParts = String(window.location.pathname || '/')
    .split('/')
    .filter(Boolean);
  const leading = pathParts[0];
  const isLocale = leading === 'en' || leading === 'zh' || leading === 'es';
  const route = isLocale ? pathParts[1] || '' : pathParts[0] || '';
  const isSimuRoute = route === 'simu';

  if (isSimuRoute) {
    return (
      <MotorStudioProvider value={studio}>
        <SimuPage />
      </MotorStudioProvider>
    );
  }

  return (
    <MotorStudioProvider value={studio}>
      <HandGatewayProvider>
      <div className="app shell">
        <BrandWatermark />
        <HeaderBar />

        {studio.workspace.menuOpen && <QuickMenu />}

        <section className="card glass">
          <div className="row toolbar compactToolbar">
            <button className={page === 'combined' ? 'primary' : ''} onClick={() => setPage('combined')}>
              {t('page_combined')}
            </button>
            <button className={page === 'tasks' ? 'primary' : ''} onClick={() => setPage('tasks')}>
              {t('page_tasks')}
            </button>
            <button className="ghostBtn" onClick={() => setHelpOpen(true)}>
              {t('help_show')}
            </button>
          </div>
        </section>

        <HelpCenterModal open={helpOpen} page={page} onClose={() => setHelpOpen(false)} />
        <ConfirmDialog
          open={Boolean(studio.scan?.confirmDialog?.open)}
          title={studio.scan?.confirmDialog?.title}
          message={studio.scan?.confirmDialog?.message}
          danger={Boolean(studio.scan?.confirmDialog?.danger)}
          onCancel={() => studio.scan?.closeConfirmDialog(false)}
          onConfirm={() => studio.scan?.closeConfirmDialog(true)}
        />

        {/* No global connection panel: the Combined page owns both gateways, so connecting
            happens in one place instead of on top of every view. */}
        {page === 'combined' && <CombinedPage />}
        {page === 'tasks' && <TasksPage />}

        <StateLogsPanel />
        <div className="appFooterMeta">{version}</div>
      </div>
      </HandGatewayProvider>
    </MotorStudioProvider>
  );
}
