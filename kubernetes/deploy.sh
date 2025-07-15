kubectl apply -f deployment-api-centrale.yaml \
              -f deployment-api-marker.yaml \
              -f deployment-marker-proxy.yaml \
              -f service-api-centrale.yaml \
              -f service-api-marker.yaml \
              -f service-marker-proxy.yaml \
              -f ingress-api-centrale.yaml \ 
              -f ingress-api-marker.yaml \ 
              -f ingress-proxy.yaml

kubectl delete secret app-env -n projet-extraction-tableaux
kubectl create secret generic app-env \
  --from-env-file=./.env \
  --namespace=projet-extraction-tableaux


kubectl delete -f deployment-api-centrale.yaml      \
               -f deployment-api-marker.yaml        \
               -f deployment-marker-proxy.yaml      \
               -f service-api-centrale.yaml         \
               -f service-api-marker.yaml           \
               -f service-marker-proxy.yaml         \
               -f ingress-api-centrale.yaml \ 
               -f ingress-api-marker.yaml \ 
               -f ingress-proxy.yaml
               --namespace projet-extraction-tableaux