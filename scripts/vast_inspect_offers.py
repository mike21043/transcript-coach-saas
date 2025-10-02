#!/usr/bin/env python3
"""Inspect Vast offers using the consolidated helpers in vast_agent.vast_utils.

This script prints categorized offer lists for quick diagnosis.
"""
import os
import sys
from vast_agent.vast_utils import search_offers, get_offer_price


def summarize(o):
    p = get_offer_price(o)
    price_str = f"${p:.3f}" if p is not None else 'n/a'
    gpu_name = o.get('gpu_name') or o.get('gpu_model') or o.get('gpu') or o.get('machine_gpu')
    return f"id={o.get('id')} price={price_str} gpus={o.get('num_gpus')} gpu_name={gpu_name} cuda_max_good={o.get('cuda_max_good')} loc={o.get('machine_country') or o.get('geolocation')}"


def main():
    key = os.getenv('VAST_API_KEY')
    if not key:
        print('VAST_API_KEY required in env')
        sys.exit(2)
    offers = search_offers(key)
    print(f'Found {len(offers)} offers after basic filtering')
    # print top 20 by price
    def price_key(x):
        p = get_offer_price(x)
        return p if p is not None else 1e9
    for o in sorted(offers, key=price_key)[:20]:
        print('  ' + summarize(o))


if __name__ == '__main__':
    main()

    full_matches = []
    model_only = []

    for o in offers:
        p = price_of(o)
        try:
            cuda = float(o.get('cuda_max_good') or 0.0)
        except:
            cuda = 0.0
        txt = model_text(o)
        has_model = any(m in txt for m in MODELS)
        if has_model and p is not None and p <= PRICE_CAP and cuda >= MIN_CUDA:
            full_matches.append(o)
        elif has_model:
            model_only.append({'offer': o, 'price': p, 'cuda_max_good': cuda})

    print('\n=== Full matches (model in {models} + cuda >= {min_cuda} + price <= ${price}) ==='.format(models=','.join(MODELS), min_cuda=MIN_CUDA, price=PRICE_CAP))
    if full_matches:
        for o in sorted(full_matches, key=lambda x: price_of(x) or 1e9):
            print('id={id} price={price:.4f} gpus={g} loc={loc} gpu_name={gn} cuda={cuda}'.format(
                id=o.get('id'), price=(price_of(o) or 0.0), g=o.get('num_gpus'), loc=o.get('machine_country') or o.get('geolocation') or o.get('country'), gn=o.get('gpu_name') or o.get('gpu_name') or '', cuda=o.get('cuda_max_good')))
    else:
        print('(none)')

    print('\n=== Model-only matches (model present but cuda or price not acceptable) ===')
    if model_only:
        for m in sorted(model_only, key=lambda x: (x['price'] if x['price'] is not None else 1e9))[:20]:
            o = m['offer']
            print('id={id} price={price} gpus={g} loc={loc} gpu_name={gn} cuda={cuda}'.format(
                id=o.get('id'), price=m['price'], g=o.get('num_gpus'), loc=o.get('machine_country') or o.get('geolocation') or o.get('country'), gn=o.get('gpu_name') or o.get('gpu') or o.get('gpu_name') or '', cuda=m['cuda_max_good']))
    else:
        print('(none)')

    print('\n=== Top 20 offers by price (for inspection) ===')
    top = sorted(offers, key=lambda x: (price_of(x) if price_of(x) is not None else 1e9))[:20]
    for o in top:
        print('\n---')
        #!/usr/bin/env python3
        """Inspect Vast offers using the consolidated helpers in vast_agent.vast_utils.

        This script prints a short list of candidate offers for human inspection.
        """
        import os
        import sys
        from vast_agent.vast_utils import search_offers, get_offer_price


        def summarize(o):
            p = get_offer_price(o)
            price_str = f"${p:.3f}" if p is not None else 'n/a'
            gpu_name = o.get('gpu_name') or o.get('gpu_model') or o.get('gpu') or o.get('machine_gpu')
            return f"id={o.get('id')} price={price_str} gpus={o.get('num_gpus')} gpu_name={gpu_name} cuda_max_good={o.get('cuda_max_good')} loc={o.get('machine_country') or o.get('geolocation')}"


        def main():
            key = os.getenv('VAST_API_KEY')
            if not key:
                print('VAST_API_KEY required in env')
                sys.exit(2)
            offers = search_offers(key)
            print(f'Found {len(offers)} offers after basic filtering')
            def price_key(x):
                p = get_offer_price(x)
                return p if p is not None else 1e9
            for o in sorted(offers, key=price_key)[:20]:
                print('  ' + summarize(o))


        if __name__ == '__main__':
            main()
