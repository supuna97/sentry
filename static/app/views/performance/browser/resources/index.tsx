import {browserHistory} from 'react-router';
import styled from '@emotion/styled';

import Breadcrumbs from 'sentry/components/breadcrumbs';
import FeatureBadge from 'sentry/components/featureBadge';
import * as Layout from 'sentry/components/layouts/thirds';
import {DatePageFilter} from 'sentry/components/organizations/datePageFilter';
import PageFilterBar from 'sentry/components/organizations/pageFilterBar';
import {ProjectPageFilter} from 'sentry/components/organizations/projectPageFilter';
import {TabList, Tabs} from 'sentry/components/tabs';
import {t} from 'sentry/locale';
import {space} from 'sentry/styles/space';
import {useLocation} from 'sentry/utils/useLocation';
import useOrganization from 'sentry/utils/useOrganization';
import {normalizeUrl} from 'sentry/utils/withDomainRequired';
import ImageView from 'sentry/views/performance/browser/resources/imageView';
import JSCSSView from 'sentry/views/performance/browser/resources/jsCssView';
import {
  BrowserStarfishFields,
  useResourceModuleFilters,
} from 'sentry/views/performance/browser/resources/utils/useResourceFilters';
import {ModulePageProviders} from 'sentry/views/performance/database/modulePageProviders';

const {RESOURCE_TYPE} = BrowserStarfishFields;

function ResourcesLandingPage() {
  const organization = useOrganization();
  const location = useLocation();
  const filters = useResourceModuleFilters();

  return (
    <ModulePageProviders title={[t('Performance'), t('Resources')].join(' — ')}>
      <Layout.Header>
        <Layout.HeaderContent>
          <Breadcrumbs
            crumbs={[
              {
                label: 'Performance',
                to: normalizeUrl(`/organizations/${organization.slug}/performance/`),
                preservePageFilters: true,
              },
              {
                label: 'Resources',
              },
            ]}
          />

          <Layout.Title>
            {t('Resources')}
            <FeatureBadge type="alpha" />
          </Layout.Title>
        </Layout.HeaderContent>
        <StyledTabs>
          <TabList
            hideBorder
            onSelectionChange={key => {
              browserHistory.push({
                ...location,
                query: {
                  ...location.query,
                  [RESOURCE_TYPE]: key,
                },
              });
            }}
          >
            <TabList.Item key="">{t('JS/CSS/Fonts')}</TabList.Item>
            <TabList.Item key="resource.img">{t('Images')}</TabList.Item>
          </TabList>
        </StyledTabs>
      </Layout.Header>

      <Layout.Body>
        <Layout.Main fullWidth>
          <PaddedContainer>
            <PageFilterBar condensed>
              <ProjectPageFilter />
              <DatePageFilter />
            </PageFilterBar>
          </PaddedContainer>

          {(!filters[RESOURCE_TYPE] ||
            filters[RESOURCE_TYPE] === 'resource.script' ||
            filters[RESOURCE_TYPE] === 'resource.css') && <JSCSSView />}

          {filters[RESOURCE_TYPE] === 'resource.img' && <ImageView />}
        </Layout.Main>
      </Layout.Body>
    </ModulePageProviders>
  );
}

export const PaddedContainer = styled('div')`
  margin-bottom: ${space(2)};
`;

const StyledTabs = styled(Tabs)`
  grid-column: 1/-1;
`;

export default ResourcesLandingPage;
