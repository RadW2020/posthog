import { urls } from 'scenes/urls'

import { ProductItemCategory, ProductKey } from '~/queries/schema/schema-general'

import { FileSystemIconColor, ProductManifest } from '../../frontend/src/types'

export const manifest: ProductManifest = {
    name: 'Deployments',
    scenes: {
        Deployments: {
            import: () => import('./frontend/Deployments'),
            name: 'Deployments',
            iconType: 'deployments',
            projectBased: true,
            description: 'View, redeploy, and roll back deployments of your app.',
        },
    },
    routes: {
        '/deployments': ['Deployments', 'deployments'],
    },
    urls: {
        deployments: (): string => '/deployments',
    },
    fileSystemTypes: {
        deployments: {
            name: 'Deployment',
            iconType: 'deployments',
            iconColor: ['var(--color-product-deployments-light)'] as FileSystemIconColor,
            href: () => urls.deployments(),
            filterKey: 'deployments',
        },
    },
    treeItemsProducts: [
        {
            path: 'Deployments',
            intents: [ProductKey.DEPLOYMENTS],
            href: urls.deployments(),
            type: 'deployments',
            category: ProductItemCategory.TOOLS,
            iconType: 'deployments',
            iconColor: ['var(--color-product-deployments-light)'] as FileSystemIconColor,
            sceneKey: 'Deployments',
        },
    ],
}
