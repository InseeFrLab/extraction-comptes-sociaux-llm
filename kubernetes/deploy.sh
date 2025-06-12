kubectl apply -f deployment-api-centrale.yaml \
              -f deployment-api-marker.yaml \
              -f deployment-marker-proxy.yaml \
              -f service-api-centrale.yaml \
              -f service-api-marker.yaml \
              -f service-marker-proxy.yaml \
              -f ingress-extraction-tableau.yaml

kubectl create secret generic app-env \
  --from-env-file=./.env \
  --namespace projet-extraction-tableaux


kubectl delete -f deployment-api-centrale.yaml      \
               -f deployment-api-marker.yaml        \
               -f deployment-marker-proxy.yaml      \
               -f service-api-centrale.yaml         \
               -f service-api-marker.yaml           \
               -f service-marker-proxy.yaml         \
               -f ingress-extraction-tableau.yaml   \
               --namespace projet-extraction-tableaux