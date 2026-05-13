import { useState } from 'react'

import { LemonSkeleton } from 'lib/lemon-ui/LemonSkeleton/LemonSkeleton'

interface DeploymentPreviewImageProps {
    src: string
    alt: string
    className?: string
}

export function DeploymentPreviewImage({ src, alt, className }: DeploymentPreviewImageProps): JSX.Element {
    const [loading, setLoading] = useState(!!src)
    const [errored, setErrored] = useState(false)
    const showImage = !!src && !errored

    return (
        <div className={`relative overflow-hidden bg-surface-secondary rounded ${className ?? ''}`}>
            {(loading || !showImage) && <LemonSkeleton className="absolute inset-0 w-full h-full" />}
            {showImage && (
                <img
                    src={src}
                    alt={alt}
                    className="w-full h-full object-cover"
                    onLoad={() => setLoading(false)}
                    onError={() => {
                        setLoading(false)
                        setErrored(true)
                    }}
                />
            )}
        </div>
    )
}
