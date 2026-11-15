package infer

import (
	"fmt"

	"ready-serving-layout-trap-v4/app/internal/bootstrap"
)

type Request struct {
	Instances [][]float64 `json:"instances"`
}

type Response struct {
	Predictions [][]float64 `json:"predictions"`
}

func Predict(state *bootstrap.AppState, request Request) (Response, error) {
	if len(request.Instances) == 0 {
		return Response{}, fmt.Errorf("instances must not be empty")
	}
	if err := state.ReadLatePredictArtifact(); err != nil {
		return Response{}, err
	}

	predictions := make([][]float64, 0, len(request.Instances))
	for _, instance := range request.Instances {
		var sum float64
		for _, value := range instance {
			sum += value
		}
		predictions = append(predictions, []float64{sum})
	}
	return Response{Predictions: predictions}, nil
}
